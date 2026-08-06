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
        skill_sections: Iterable[dict[str, Any]] = (),
        working_plan: dict[str, Any] | None = None,
        pending_results: Iterable[dict[str, Any]] = (),
        token_budget: int = 24_000,
    ) -> ContextPackage:
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
        for skill in skills:
            entry = {
                "role": "system",
                "content": f"Skill {skill.get('name', '')}@{skill.get('version_id', '')}:\n{skill.get('content', '')}",
            }
            messages.append(entry)
            manifest.append({
                "source": "skill", "version": skill.get("version_id"),
                "checksum": skill.get("checksum") or _digest(entry),
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
        estimate = sum(_estimate(item) for item in messages)
        if estimate <= token_budget:
            return ContextPackage(tuple(messages), tuple(manifest), estimate)

        # Preserve system policy, the current goal/input, pending action results,
        # and the newest history. The summary is a stable fact index, not hidden
        # reasoning and not a second business state machine.
        fixed = messages[:1 + len(skills) + (1 if working_plan else 0)]
        # Pending Tool/Agent results are authoritative continuation state and
        # are never compacted away. If they alone exceed the budget, the
        # package may exceed its target rather than silently lose state.
        recent_budget = max(
            0,
            token_budget
            - sum(_estimate(item) for item in fixed)
            - sum(_estimate(item) for item in pending)
            - _estimate({"role": "user", "content": current_input})
            - 512,
        )
        recent: list[dict[str, Any]] = []
        for entry in reversed(history_messages):
            if sum(_estimate(item) for item in recent) + _estimate(entry) > recent_budget:
                continue
            recent.insert(0, entry)
        omitted = [entry for entry in history_messages if entry not in recent]
        summary = {
            "omitted_count": len(omitted),
            "source_hashes": [_digest(entry) for entry in omitted],
            "latest_roles": [str(entry.get("role")) for entry in omitted[-8:]],
            "working_plan_revision": (working_plan or {}).get("revision"),
        }
        compacted = fixed + [{
            "role": "system",
            "content": "Compacted context fact index: " + json.dumps(summary, sort_keys=True),
        }] + recent + [{"role": "user", "content": current_input}] + pending
        final_estimate = sum(_estimate(item) for item in compacted)
        return ContextPackage(
            tuple(compacted), tuple(manifest), final_estimate,
            compaction={**summary, "content_hash": _digest(summary)},
        )
