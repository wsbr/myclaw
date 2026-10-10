from typing import AsyncIterator
import json
import openai as oai
from app.core.model_hub.base import BaseLLM, LLMResponse, LLMChunk, ToolCall, TokenUsage
from app.core.model_hub.base import bounded_provider_id, provider_request_identity
from app.core.model_hub.tool_wire import to_openai_tools
from app.core.model_hub.parameters import validate_provider_parameters
from app.runtime.protocol import ProviderSchemaError, Usage


def _token_usage(usage) -> TokenUsage:
    details = getattr(usage, "prompt_tokens_details", None)
    return TokenUsage(
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        cached_input_tokens=getattr(details, "cached_tokens", None),
    )


def _tool_schema_error(
    message: str, *, arguments: str, tool_index: int, usage,
    stop_reason: str | None, provider_request_id: str | None,
    parse_error: json.JSONDecodeError | None = None,
) -> ProviderSchemaError:
    # Keep only sizes/locations, never the argument buffer or JSONDecodeError.doc.
    diagnostics = {
        "tool_index": tool_index,
        "argument_bytes": len(arguments.encode("utf-8")),
    }
    if parse_error is not None:
        diagnostics.update({
            "json_error_position": parse_error.pos,
            "json_error_line": parse_error.lineno,
            "json_error_column": parse_error.colno,
        })
    return ProviderSchemaError(
        message, stop_reason=stop_reason,
        usage=Usage(**_token_usage(usage).model_dump(exclude_none=True)),
        provider_request_id=provider_request_id, diagnostics=diagnostics,
    )


def normalize_messages_for_openai(messages: list[dict]) -> list[dict]:
    """Convert internal tool-call messages to OpenAI chat completion format."""
    normalized: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        if role == "assistant" and msg.get("turn_items"):
            content_parts = []
            tool_calls = []
            for item in msg["turn_items"]:
                if item.get("type") == "assistant_message":
                    content_parts.extend(
                        str(part.get("value", ""))
                        for part in item.get("content_parts", [])
                        if part.get("type") == "text"
                    )
                elif item.get("type") == "tool_call":
                    tool_calls.append({
                        "id": item["call_id"], "type": "function",
                        "function": {
                            "name": item["name"],
                            "arguments": json.dumps(
                                item.get("arguments") or {}, ensure_ascii=False,
                            ),
                        },
                    })
            normalized.append({
                "role": "assistant", "content": "".join(content_parts),
                "tool_calls": tool_calls,
            })
            continue
        if role == "assistant" and msg.get("tool_calls"):
            tool_calls = []
            for tc in msg["tool_calls"]:
                if tc.get("function"):
                    tool_calls.append(tc)
                    continue
                args = tc.get("input", tc.get("arguments", {}))
                if isinstance(args, str):
                    args_str = args
                else:
                    args_str = json.dumps(args, ensure_ascii=False)
                tool_calls.append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": args_str,
                    },
                })
            normalized.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": tool_calls,
            })
        elif role == "tool" or msg.get("tool_use_id"):
            normalized.append({
                "role": "tool",
                "tool_call_id": msg.get("tool_use_id") or msg.get("tool_call_id"),
                "content": msg.get("content", ""),
            })
        else:
            normalized.append(msg)
    return normalized


def _is_retryable_zero_output_error(error: Exception) -> bool:
    """Classify transport/provider failures that are safe before any delta."""
    status_code = getattr(error, "status_code", None)
    if status_code in {408, 409, 429} or (isinstance(status_code, int) and status_code >= 500):
        return True
    message = str(error).lower()
    return any(marker in message for marker in (
        "request timed out", "timeout", "timed out", "temporarily unavailable",
        "connection reset", "connection error", "peer closed connection",
        "incomplete chunked read",
    ))


class OpenAILLM(BaseLLM):
    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        *,
        tool_strict: bool = False,
    ):
        self.client = oai.AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        self.tool_strict = tool_strict

    async def chat(self, messages: list[dict], tools: list[dict] | None, params: dict) -> LLMResponse:
        validate_provider_parameters(params, provider="openai")
        msgs = normalize_messages_for_openai(messages)

        kwargs = dict(
            model=params.get("model_id", "gpt-4o"),
            messages=msgs,
            max_tokens=params.get("max_tokens", 8192),
            temperature=params.get("temperature", 0.7),
        )
        if tools:
            kwargs["tools"] = to_openai_tools(tools, strict=self.tool_strict)
            if params.get("tool_choice"):
                kwargs["tool_choice"] = params["tool_choice"]
        if params.get("response_format"):
            kwargs["response_format"] = params["response_format"]
        if isinstance(params.get("extra_body"), dict):
            kwargs["extra_body"] = dict(params["extra_body"])

        resp = await self.client.chat.completions.create(**kwargs)
        request_id, request_id_source = provider_request_identity(resp)
        response_id = bounded_provider_id(getattr(resp, "id", None))
        choice = resp.choices[0]
        msg = choice.message
        tool_calls = []
        if msg.tool_calls:
            for index, tc in enumerate(msg.tool_calls):
                try:
                    arguments = json.loads(tc.function.arguments)
                except json.JSONDecodeError as error:
                    raise _tool_schema_error(
                        "Provider returned malformed Tool arguments",
                        arguments=tc.function.arguments, tool_index=index,
                        usage=resp.usage, stop_reason=choice.finish_reason,
                        provider_request_id=getattr(resp, "id", None),
                        parse_error=error,
                    ) from error
                if not isinstance(arguments, dict):
                    raise _tool_schema_error(
                        "Provider Tool arguments must be a JSON object",
                        arguments=tc.function.arguments, tool_index=index,
                        usage=resp.usage, stop_reason=choice.finish_reason,
                        provider_request_id=getattr(resp, "id", None),
                    )
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=arguments))

        return LLMResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            usage=_token_usage(resp.usage),
            model=resp.model,
            finish_reason=choice.finish_reason,
            provider_request_id=request_id or response_id,
            provider_request_id_source=request_id_source or ("response_id_fallback" if response_id else None),
            provider_response_id=response_id,
        )

    async def stream(self, messages: list[dict], tools: list[dict] | None, params: dict) -> AsyncIterator[LLMChunk]:
        validate_provider_parameters(params, provider="openai")
        msgs = normalize_messages_for_openai(messages)

        kwargs = dict(
            model=params.get("model_id", "gpt-4o"),
            messages=msgs,
            stream=True,
            stream_options={"include_usage": True},
            max_tokens=params.get("max_tokens", 8192),
            temperature=params.get("temperature", 0.7),
        )
        if tools:
            kwargs["tools"] = to_openai_tools(tools, strict=self.tool_strict)
            if params.get("tool_choice"):
                kwargs["tool_choice"] = params["tool_choice"]
        if params.get("response_format"):
            kwargs["response_format"] = params["response_format"]
        if isinstance(params.get("extra_body"), dict):
            kwargs["extra_body"] = dict(params["extra_body"])

        # Transport retry is intentionally owned by ModelAdapter so each
        # physical request has a durable ProviderAttempt. The provider-specific
        # layer only assembles wire chunks.
        tool_call_acc: dict[int, dict] = {}
        usage = None
        finish_reason = None
        response_model = None
        provider_request_id = None
        provider_request_id_source = None
        provider_response_id = None
        next_item_index = 0
        text_item_index = None
        stream = await self.client.chat.completions.create(**kwargs)
        stream_failed = False
        try:
            provider_request_id, provider_request_id_source = provider_request_identity(
                getattr(stream, "response", None),
            )
            if provider_request_id:
                yield LLMChunk(
                    type="metadata", provider_request_id=provider_request_id,
                    provider_request_id_source=provider_request_id_source,
                )
            async for chunk in stream:
                response_model = getattr(chunk, "model", None) or response_model
                response_id = bounded_provider_id(getattr(chunk, "id", None))
                if response_id and response_id != provider_response_id:
                    provider_response_id = response_id
                    yield LLMChunk(
                        type="metadata", provider_request_id=provider_request_id,
                        provider_request_id_source=provider_request_id_source,
                        provider_response_id=provider_response_id,
                    )
                if chunk.usage:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                finish_reason = getattr(choice, "finish_reason", None) or finish_reason
                delta = choice.delta
                if delta and delta.content:
                    if text_item_index is None:
                        text_item_index = next_item_index
                        next_item_index += 1
                    yield LLMChunk(
                        type="text_delta", content=delta.content,
                        item_index=text_item_index,
                    )
                if delta and delta.tool_calls:
                    # Buffered actionable Tool JSON fences replay; metadata does not.
                    yield LLMChunk(type="transport_payload")
                    for tc_delta in delta.tool_calls:
                        if tc_delta.index not in tool_call_acc:
                            tool_call_acc[tc_delta.index] = {
                                "id": None, "name": None, "arguments": "",
                                "item_index": next_item_index,
                            }
                            next_item_index += 1
                        entry = tool_call_acc[tc_delta.index]
                        if tc_delta.id:
                            entry["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                entry["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                entry["arguments"] += tc_delta.function.arguments
        except BaseException:
            stream_failed = True
            raise
        finally:
            # Release the HTTP response on early EOF as well as successful EOF.
            close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    if not stream_failed:
                        raise

        for idx in sorted(tool_call_acc):
            entry = tool_call_acc[idx]
            try:
                arguments = json.loads(entry["arguments"] or "{}")
            except json.JSONDecodeError as error:
                raise _tool_schema_error(
                    "Provider returned malformed streamed Tool arguments",
                    arguments=entry["arguments"], tool_index=idx,
                    usage=usage, stop_reason=finish_reason,
                    provider_request_id=provider_request_id or provider_response_id, parse_error=error,
                ) from error
            if not isinstance(arguments, dict):
                raise _tool_schema_error(
                    "Provider streamed Tool arguments must be a JSON object",
                    arguments=entry["arguments"], tool_index=idx,
                    usage=usage, stop_reason=finish_reason,
                    provider_request_id=provider_request_id or provider_response_id,
                )
            yield LLMChunk(
                type="tool_call_delta",
                tool_call=ToolCall(id=entry["id"] or "", name=entry["name"] or "", arguments=arguments),
                item_index=entry["item_index"],
            )

        yield LLMChunk(
            type="done",
            usage=_token_usage(usage),
            stop_reason=finish_reason,
            model=response_model,
            provider_request_id=provider_request_id,
            provider_request_id_source=provider_request_id_source,
            provider_response_id=provider_response_id,
        )

    async def count_tokens(
        self, messages: list[dict], tools: list[dict] | None = None,
        params: dict | None = None,
    ) -> int:
        """Count the complete request with OpenAI's model tokenizer."""

        params = dict(params or {})
        validate_provider_parameters(params, provider="openai")
        import tiktoken

        model = str(params.get("model_id") or "gpt-4o")
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("o200k_base")
        normalized = normalize_messages_for_openai(messages)
        token_count = 3
        for message in normalized:
            token_count += 3 + len(encoding.encode(json.dumps(
                message, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )))
        if tools:
            token_count += len(encoding.encode(json.dumps(
                to_openai_tools(tools, strict=self.tool_strict),
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ))) + 12
        if params.get("response_format"):
            token_count += len(encoding.encode(json.dumps(
                params["response_format"], ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )))
        return max(1, token_count)
