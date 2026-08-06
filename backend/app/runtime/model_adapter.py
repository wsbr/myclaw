"""Unified streaming boundary for every supported model provider."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable

from app.core.model_hub.base import BaseLLM
from app.runtime.protocol import (
    AssistantMessageItem,
    ContentPart,
    NormalizedTurnResult,
    ProviderAttemptEvent,
    ProviderError,
    ProviderRequest,
    ProviderSchemaError,
    StreamEvent,
    ToolCallItem,
    Usage,
    normalize_stop_reason,
)


AttemptObserver = Callable[[ProviderAttemptEvent], Awaitable[None] | None]
StreamObserver = Callable[[StreamEvent], Awaitable[None] | None]


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _retryable_transport(error: Exception) -> bool:
    if isinstance(error, ProviderError):
        return error.retryable
    status_code = getattr(error, "status_code", None)
    if status_code in {408, 409, 429} or isinstance(status_code, int) and status_code >= 500:
        return True
    message = str(error).lower()
    return any(marker in message for marker in (
        "timeout", "timed out", "temporarily unavailable", "connection reset",
        "connection error", "peer closed connection", "incomplete chunked read",
    ))


def _classify_provider_error(error: Exception, *, received_payload: bool) -> ProviderError:
    if isinstance(error, ProviderError):
        return error
    status_code = getattr(error, "status_code", None)
    message = str(error)
    normalized = message.lower()
    if status_code in {401, 403}:
        code, retryable = "provider_authentication_error", False
    elif status_code == 429:
        code, retryable = "provider_rate_limit", True
    elif status_code == 408 or "timeout" in normalized or "timed out" in normalized:
        code, retryable = "provider_timeout", True
    elif "context length" in normalized or "context window" in normalized:
        code, retryable = "provider_context_length", False
    elif any(marker in normalized for marker in (
        "connection reset", "connection error", "peer closed connection",
        "incomplete chunked read", "temporarily unavailable",
    )):
        code, retryable = "provider_transport_error", True
    elif isinstance(status_code, int) and status_code >= 500:
        code, retryable = "provider_internal_error", True
    else:
        code, retryable = "provider_internal_error", False
    return ProviderError(
        message, code=code, retryable=retryable,
        received_payload=received_payload,
    )


async def _notify(callback, value) -> None:
    if callback is None:
        return
    returned = callback(value)
    if inspect.isawaitable(returned):
        await returned


class ModelAdapter:
    """Maps a provider SDK adapter into ordered Agent-native Turn Items."""

    def __init__(self, provider: BaseLLM, *, max_zero_payload_retries: int = 1) -> None:
        self.provider = provider
        self.max_zero_payload_retries = max(0, max_zero_payload_retries)

    async def invoke(
        self,
        request: ProviderRequest,
        *,
        attempt_observer: AttemptObserver | None = None,
        stream_observer: StreamObserver | None = None,
    ) -> NormalizedTurnResult:
        request_hash = _canonical_hash({
            "messages": request.messages,
            "tools": request.tools,
            "parameters": request.parameters,
            "provider_state_ref": request.provider_state_ref,
        })
        for attempt_no in range(1, self.max_zero_payload_retries + 2):
            received_payload = False
            text_parts: dict[int, list[str]] = {}
            tool_calls: list[ToolCallItem] = []
            next_item_index = 0
            default_text_index: int | None = None
            usage = Usage()
            stop_reason = None
            model = None
            provider_request_id = None
            provider_state_ref = request.provider_state_ref
            await _notify(attempt_observer, ProviderAttemptEvent(
                phase="started", attempt_no=attempt_no, received_payload=False,
                request_hash=request_hash,
            ))
            try:
                async for chunk in self.provider.stream(
                    list(request.messages), list(request.tools) or None,
                    dict(request.parameters),
                ):
                    if chunk.type == "transport_payload":
                        received_payload = True
                    elif chunk.type == "text_delta" and chunk.content is not None:
                        received_payload = True
                        if chunk.item_index is not None:
                            item_index = chunk.item_index
                            next_item_index = max(next_item_index, item_index + 1)
                        else:
                            if default_text_index is None:
                                default_text_index = next_item_index
                                next_item_index += 1
                            item_index = default_text_index
                        text_parts.setdefault(item_index, []).append(chunk.content)
                        await _notify(stream_observer, StreamEvent(
                            type="text_delta", data={"content": chunk.content},
                        ))
                    elif chunk.type == "tool_call_delta" and chunk.tool_call is not None:
                        received_payload = True
                        if not isinstance(chunk.tool_call.arguments, dict):
                            raise ProviderSchemaError("Tool arguments must be a JSON object")
                        item_index = (
                            chunk.item_index if chunk.item_index is not None
                            else next_item_index
                        )
                        next_item_index = max(next_item_index, item_index + 1)
                        item = ToolCallItem(
                            call_id=chunk.tool_call.id,
                            name=chunk.tool_call.name,
                            arguments=chunk.tool_call.arguments,
                            index=item_index,
                        )
                        tool_calls.append(item)
                        await _notify(stream_observer, StreamEvent(
                            type="tool_call", data=item.as_dict(),
                        ))
                    elif chunk.type == "done":
                        if chunk.usage is not None:
                            usage = Usage(
                                input_tokens=chunk.usage.input_tokens,
                                output_tokens=chunk.usage.output_tokens,
                            )
                        stop_reason = getattr(chunk, "stop_reason", None)
                        model = getattr(chunk, "model", None)
                        provider_request_id = getattr(chunk, "provider_request_id", None)
                        provider_state_ref = (
                            getattr(chunk, "provider_state_ref", None) or provider_state_ref
                        )
                ordered_items = [
                    (index, AssistantMessageItem((
                        ContentPart(type="text", value="".join(parts)),
                    )))
                    for index, parts in text_parts.items()
                ]
                ordered_items.extend((item.index, item) for item in tool_calls)
                items = [item for _, item in sorted(ordered_items, key=lambda value: value[0])]
                result = NormalizedTurnResult(
                    items=tuple(items),
                    stop_reason=normalize_stop_reason(
                        stop_reason, has_tool_calls=bool(tool_calls)
                    ),
                    usage=usage,
                    model=model,
                    provider_request_id=provider_request_id,
                    provider_state_ref=provider_state_ref,
                )
                await _notify(attempt_observer, ProviderAttemptEvent(
                    phase="succeeded", attempt_no=attempt_no,
                    received_payload=received_payload, request_hash=request_hash,
                    result=result,
                ))
                await _notify(stream_observer, StreamEvent(
                    type="done", data=result.as_dict(),
                ))
                return result
            except Exception as error:
                received_payload = received_payload or bool(
                    getattr(error, "received_payload", False)
                )
                await _notify(attempt_observer, ProviderAttemptEvent(
                    phase="failed", attempt_no=attempt_no,
                    received_payload=received_payload, request_hash=request_hash,
                    error=error,
                ))
                can_retry = (
                    attempt_no <= self.max_zero_payload_retries
                    and not received_payload
                    and _retryable_transport(error)
                )
                if can_retry:
                    continue
                classified = _classify_provider_error(
                    error, received_payload=received_payload,
                )
                raise classified from error
        raise AssertionError("unreachable")
