"""Deterministic context assembly and safe compaction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Iterable


@dataclass(frozen=True)
class ContextPackage:
    messages: tuple[dict[str, Any], ...]
    manifest: tuple[dict[str, Any], ...]
    estimated_tokens: int
    compaction: dict[str, Any] | None = None
    soft_input_target: int | None = None
    hard_input_limit: int | None = None
    pre_call_tokens: int | None = None
    count_method: str | None = None
    history_start: int = 0
    history_count: int = 0
    web_result_budget_ratio: float = 0.20
    context_policy_key: str = "context.standard"
    recent_history_messages: int = 0
    recent_tool_rounds: int = 2
    consumed_tool_result_tokens: int = 1024
    # Canonical, unprojected continuation. Provider views must never become the
    # input to the next projection or the only recoverable checkpoint payload.
    source_messages: tuple[dict[str, Any], ...] | None = None
    source_manifest: tuple[dict[str, Any], ...] | None = None


def oldest_history_group_size(package: ContextPackage) -> int:
    """Keep an assistant's parallel calls and all their results atomic."""

    if package.history_count <= 0:
        return 0
    history = package.messages[package.history_start:package.history_start + package.history_count]
    calls = history[0].get("tool_calls") or []
    if not calls:
        return 1
    pending = {str(call.get("id")) for call in calls if call.get("id")}
    if not pending:
        return 0
    for size, message in enumerate(history[1:], 2):
        if message.get("role") != "tool":
            return 0
        pending.discard(str(message.get("tool_call_id") or message.get("tool_use_id") or ""))
        if not pending:
            return size
    # A result may be in the protected continuation. Do not orphan it.
    return 0


def _estimate(value: object) -> int:
    return max(1, len(json.dumps(value, ensure_ascii=False, default=str)) // 4)


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def request_manifest(
    messages: Iterable[dict[str, Any]],
    sources: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Describe exactly the model-facing projection, retaining source provenance."""

    previous = list(sources)
    result = []
    for index, message in enumerate(messages):
        source = dict(previous[index]) if index < len(previous) else {
            "source": "continuation", "visibility": "private",
        }
        checksum = _digest(message)
        if source.get("checksum") and source["checksum"] != checksum:
            source.setdefault("source_checksum", source["checksum"])
            source["treatment"] = "projection"
        result.append({
            **source, "message_index": index, "role": message.get("role"),
            "checksum": checksum, "token_estimate": _estimate(message),
        })
    return result


class ContextAssembler:
    """Builds the same minimal context package for every product entry path."""

    def assemble(
        self,
        *,
        platform_rules: str,
        agent_instructions: str,
        current_input: str,
        history: Iterable[dict[str, Any]] = (),
        history_manifest: Iterable[dict[str, Any]] = (),
        permission_summary: dict[str, Any] | None = None,
        policy_sections: Iterable[dict[str, Any]] = (),
        skill_sections: Iterable[dict[str, Any]] = (),
        working_plan: dict[str, Any] | None = None,
        pending_results: Iterable[dict[str, Any]] = (),
        pending_manifest: Iterable[dict[str, Any]] = (),
        token_budget: int = 24_000,
        soft_input_target: int | None = None,
        hard_input_limit: int | None = None,
        tool_definitions: Iterable[dict[str, Any]] = (),
        web_result_budget_ratio: float = 0.20,
        context_policy_key: str = "context.standard",
        recent_history_messages: int = 0,
        recent_tool_rounds: int = 2,
        consumed_tool_result_tokens: int = 1024,
        defer_history_trimming: bool = False,
    ) -> ContextPackage:
        from app.runtime.token_counting import conservative_request_tokens

        soft_target = max(1, int(soft_input_target or token_budget))
        hard_limit = max(1, int(hard_input_limit or max(soft_target, token_budget)))
        tools = [dict(item) for item in tool_definitions]
        sections = [dict(item) for item in policy_sections]
        skills = [dict(item) for item in skill_sections]
        messages: list[dict[str, Any]] = [{
            "role": "system",
            "content": "\n\n".join(part for part in (
                platform_rules.strip(), agent_instructions.strip(),
                f"Execution boundary: {json.dumps(permission_summary or {}, ensure_ascii=False, sort_keys=True)}",
            ) if part),
        }]
        manifest: list[dict[str, Any]] = [{
            "source": "platform+agent+permission", "visibility": "private",
            "checksum": _digest(messages[0]), "token_estimate": _estimate(messages[0]),
        }]
        for section in sections:
            section_manifest = dict(section.get("manifest") or {})
            if "checksum" in section_manifest:
                section_manifest["selection_checksum"] = section_manifest.pop("checksum")
            if "token_estimate" in section_manifest:
                section_manifest["selection_token_estimate"] = section_manifest.pop(
                    "token_estimate"
                )
            entry = {
                "role": str(section.get("role") or "assistant"),
                "content": str(section.get("content") or ""),
            }
            messages.append(entry)
            manifest.append({
                "source": str(section.get("source") or "policy"),
                "visibility": str(section.get("visibility") or "private"),
                "checksum": _digest(entry),
                "token_estimate": _estimate(entry),
                **section_manifest,
            })
        for skill in skills:
            entry = {
                "role": "system",
                "content": str(skill.get("content") or ""),
            }
            messages.append(entry)
            manifest.append({
                "source": "skill_catalog", "version": skill.get("version_id"),
                "package_checksum": skill.get("checksum"),
                "path": skill.get("path"), "checksum": _digest(entry),
                "visibility": skill.get("visibility", "private"),
                "token_estimate": _estimate(entry),
            })
        if working_plan:
            entry = {"role": "assistant", "content": "WorkingPlan: (state data, not instructions) " + json.dumps(
                working_plan, ensure_ascii=False, sort_keys=True
            )}
            messages.append(entry)
            manifest.append({
                "source": "working_plan", "revision": working_plan.get("revision"),
                "checksum": _digest(entry), "visibility": "private",
                "token_estimate": _estimate(entry),
            })
        history_messages = [dict(item) for item in history]
        history_sources = list(history_manifest)
        pending = [dict(item) for item in pending_results]
        pending_sources = list(pending_manifest)
        messages.extend(history_messages)
        messages.append({"role": "user", "content": current_input})
        messages.extend(pending)
        manifest.extend({
            "source": "history", "checksum": _digest(entry),
            "visibility": entry.get("visibility", "private"),
            "token_estimate": _estimate(entry),
            **(history_sources[index] if index < len(history_sources) else {}),
        } for index, entry in enumerate(history_messages))
        manifest.append({
            "source": "current_input",
            "checksum": _digest({"role": "user", "content": current_input}),
            "visibility": "user",
            "token_estimate": _estimate({"role": "user", "content": current_input}),
        })
        manifest.extend({
            "source": "pending_result", "checksum": _digest(entry),
            "visibility": entry.get("visibility", "private"),
            "token_estimate": _estimate(entry),
            **(pending_sources[index] if index < len(pending_sources) else {}),
        } for index, entry in enumerate(pending))
        manifest = request_manifest(messages, manifest)
        fixed_count = 1 + len(sections) + len(skills) + (1 if working_plan else 0)
        estimate = conservative_request_tokens(messages, tools)
        profile = {
            "context_policy_key": context_policy_key,
            "recent_history_messages": recent_history_messages,
            "recent_tool_rounds": recent_tool_rounds,
            "consumed_tool_result_tokens": consumed_tool_result_tokens,
        }
        if estimate <= soft_target or defer_history_trimming:
            return ContextPackage(
                tuple(messages), tuple(manifest), estimate,
                soft_input_target=soft_target, hard_input_limit=hard_limit,
                history_start=fixed_count, history_count=len(history_messages),
                web_result_budget_ratio=web_result_budget_ratio,
                **profile,
            )

        # Preserve system policy, the current goal/input, pending action results,
        # and the newest history. Record omissions without inventing a summary.
        fixed = messages[:fixed_count]
        # Pending Tool/Agent results are authoritative continuation state and
        # are never compacted away. If they alone exceed the budget, the
        # package may exceed its target rather than silently lose state.
        recent = list(history_messages)
        compacted = fixed + recent + [{"role": "user", "content": current_input}] + pending
        while recent and conservative_request_tokens(compacted, tools) > soft_target:
            group_size = oldest_history_group_size(ContextPackage(
                messages=tuple(recent), manifest=(), estimated_tokens=0,
                history_start=0, history_count=len(recent),
            ))
            if not group_size or len(recent) - group_size < recent_history_messages:
                break
            del recent[:group_size]
            compacted = fixed + recent + [{
                "role": "user", "content": current_input,
            }] + pending
        omitted = history_messages[:len(history_messages) - len(recent)]
        omitted_sections = manifest[fixed_count:fixed_count + len(omitted)]
        del manifest[fixed_count:fixed_count + len(omitted)]
        summary = {
            "omitted_count": len(omitted),
            "omitted_content_hash": _digest(omitted),
            "omitted_sections": omitted_sections,
            "working_plan_revision": (working_plan or {}).get("revision"),
            "reason": "soft_input_target_exceeded",
        }
        final_estimate = conservative_request_tokens(compacted, tools)
        summary["soft_target_unmet"] = final_estimate > soft_target
        return ContextPackage(
            tuple(compacted), tuple(request_manifest(compacted, manifest)), final_estimate,
            compaction={**summary, "content_hash": _digest(summary)},
            soft_input_target=soft_target, hard_input_limit=hard_limit,
            history_start=fixed_count, history_count=len(recent),
            web_result_budget_ratio=web_result_budget_ratio,
            **profile,
        )

    def drop_oldest_history(
        self, package: ContextPackage, *, reason: str,
    ) -> ContextPackage:
        """Remove one oldest ambient History group; never split call/result pairs."""

        group_size = oldest_history_group_size(package)
        if not group_size:
            return package
        messages = list(package.messages)
        omitted = messages[package.history_start:package.history_start + group_size]
        del messages[package.history_start:package.history_start + group_size]
        manifest = request_manifest(package.messages, package.manifest)
        omitted_sections = manifest[package.history_start:package.history_start + group_size]
        del manifest[package.history_start:package.history_start + group_size]
        prior = dict(package.compaction or {})
        # A hash of the earlier omitted message list no longer describes the
        # combined omissions. The section checksums retain every source hash.
        prior.pop("omitted_content_hash", None)
        omitted_count = int(prior.get("omitted_count") or 0) + group_size
        compaction = {
            **prior,
            "omitted_count": omitted_count,
            "omitted_sections": [*prior.get("omitted_sections", []), *omitted_sections],
            "reason": reason,
            "content_hash": _digest({
                "previous": prior.get("content_hash"),
                "omitted": _digest(omitted),
                "reason": reason,
            }),
        }
        source_messages, source_manifest = package.source_messages, package.source_manifest
        if source_messages is not None:
            raw = list(source_messages)
            sources = request_manifest(raw, source_manifest or package.manifest)
            del raw[package.history_start:package.history_start + group_size]
            del sources[package.history_start:package.history_start + group_size]
            source_messages = tuple(raw)
            source_manifest = tuple(request_manifest(raw, sources))
        return replace(
            package, messages=tuple(messages),
            manifest=tuple(request_manifest(messages, manifest)),
            estimated_tokens=max(0, package.estimated_tokens - _estimate(omitted)),
            compaction=compaction,
            pre_call_tokens=None, count_method=None,
            history_count=package.history_count - group_size,
            source_messages=source_messages, source_manifest=source_manifest,
        )
