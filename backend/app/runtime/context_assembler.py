"""Deterministic context assembly and safe compaction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
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


def _estimate(value: object) -> int:
    return max(1, len(json.dumps(value, ensure_ascii=False, default=str)) // 4)


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class ContextAssembler:
    """Builds the same minimal context package for every product entry path."""

    def assemble(
        self,
        *,
        platform_rules: str,
        agent_instructions: str,
        current_input: str,
        history: Iterable[dict[str, Any]] = (),
        permission_summary: dict[str, Any] | None = None,
        policy_sections: Iterable[dict[str, Any]] = (),
        skill_sections: Iterable[dict[str, Any]] = (),
        working_plan: dict[str, Any] | None = None,
        pending_results: Iterable[dict[str, Any]] = (),
        token_budget: int = 24_000,
        soft_input_target: int | None = None,
        hard_input_limit: int | None = None,
        tool_definitions: Iterable[dict[str, Any]] = (),
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
            entry = {"role": "system", "content": "WorkingPlan: " + json.dumps(
                working_plan, ensure_ascii=False, sort_keys=True
            )}
            messages.append(entry)
            manifest.append({
                "source": "working_plan", "revision": working_plan.get("revision"),
                "checksum": _digest(entry), "visibility": "private",
                "token_estimate": _estimate(entry),
            })
        history_messages = [dict(item) for item in history]
        pending = [dict(item) for item in pending_results]
        messages.extend(history_messages)
        messages.append({"role": "user", "content": current_input})
        messages.extend(pending)
        manifest.extend({
            "source": "history", "checksum": _digest(entry),
            "visibility": entry.get("visibility", "private"),
            "token_estimate": _estimate(entry),
        } for entry in history_messages)
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
        } for entry in pending)
        fixed_count = 1 + len(sections) + len(skills) + (1 if working_plan else 0)
        estimate = conservative_request_tokens(messages, tools)
        if estimate <= soft_target:
            return ContextPackage(
                tuple(messages), tuple(manifest), estimate,
                soft_input_target=soft_target, hard_input_limit=hard_limit,
                history_start=fixed_count, history_count=len(history_messages),
            )

        # Preserve system policy, the current goal/input, pending action results,
        # and the newest history. The summary is a stable fact index, not hidden
        # reasoning and not a second business state machine.
        fixed = messages[:fixed_count]
        # Pending Tool/Agent results are authoritative continuation state and
        # are never compacted away. If they alone exceed the budget, the
        # package may exceed its target rather than silently lose state.
        recent = list(history_messages)
        compacted = fixed + recent + [{"role": "user", "content": current_input}] + pending
        while recent and conservative_request_tokens(compacted, tools) > soft_target:
            recent.pop(0)
            compacted = fixed + recent + [{
                "role": "user", "content": current_input,
            }] + pending
        omitted = history_messages[:len(history_messages) - len(recent)]
        summary = {
            "omitted_count": len(omitted),
            "omitted_content_hash": _digest(omitted),
            "working_plan_revision": (working_plan or {}).get("revision"),
            "reason": "soft_input_target_exceeded",
        }
        final_estimate = conservative_request_tokens(compacted, tools)
        summary["soft_target_unmet"] = final_estimate > soft_target
        return ContextPackage(
            tuple(compacted), tuple(manifest), final_estimate,
            compaction={**summary, "content_hash": _digest(summary)},
            soft_input_target=soft_target, hard_input_limit=hard_limit,
            history_start=fixed_count, history_count=len(recent),
        )

    def drop_oldest_history(
        self, package: ContextPackage, *, reason: str,
    ) -> ContextPackage:
        """Remove one oldest ambient History item; immutable sections stay intact."""

        if package.history_count <= 0:
            return package
        messages = list(package.messages)
        omitted = messages.pop(package.history_start)
        prior = dict(package.compaction or {})
        omitted_count = int(prior.get("omitted_count") or 0) + 1
        compaction = {
            **prior,
            "omitted_count": omitted_count,
            "reason": reason,
            "content_hash": _digest({
                "previous": prior.get("content_hash"),
                "omitted": _digest(omitted),
                "reason": reason,
            }),
        }
        return ContextPackage(
            messages=tuple(messages), manifest=package.manifest,
            estimated_tokens=package.estimated_tokens,
            compaction=compaction,
            soft_input_target=package.soft_input_target,
            hard_input_limit=package.hard_input_limit,
            pre_call_tokens=package.pre_call_tokens,
            count_method=package.count_method,
            history_start=package.history_start,
            history_count=package.history_count - 1,
        )
