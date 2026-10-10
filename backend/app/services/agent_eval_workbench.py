"""Evaluation imports and paired comparisons over existing durable facts."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_v1 import AgentAssemblyVersion
from app.models.runtime_v1 import Message, Run, ToolInvocation, WorkItem


def source_runs_query(*, agent_id: str, tenant_id: str, account_id: str, user_id: str):
    # Owning an Agent does not grant access to other users' conversations.
    # Only single Chat inputs can be replayed faithfully by the draft-test runner.
    return select(Run, WorkItem).join(WorkItem, WorkItem.id == Run.root_work_item_id).where(
        Run.tenant_id == tenant_id, WorkItem.tenant_id == tenant_id,
        Run.account_id == account_id, Run.user_id == user_id,
        WorkItem.owner_agent_id == agent_id,
        Run.mode == "chat", Run.status.in_(("completed", "failed", "cancelled")),
    )


async def import_run_preview(
    db: AsyncSession, *, agent_id: str, tenant_id: str, account_id: str,
    user_id: str, run_id: str,
) -> dict:
    row = (await db.execute(source_runs_query(
        agent_id=agent_id, tenant_id=tenant_id, account_id=account_id, user_id=user_id,
    ).where(Run.id == run_id))).first()
    if row is None:
        raise LookupError("可导入的运行不存在或无权访问；仅支持自己的已结束 Chat 运行")
    run, work = row
    response = await db.scalar(select(Message.content).where(
        Message.run_id == run.id, Message.status == "committed",
        Message.scope_type == "chat", Message.role == "assistant",
        Message.sender_agent_id == agent_id, Message.recipient_type == "session",
    ).order_by(Message.created_at.desc(), Message.id.desc()).limit(1))
    tools = list(await db.execute(select(ToolInvocation.tool_key, ToolInvocation.status).where(
        ToolInvocation.run_id == run.id,
    ).order_by(ToolInvocation.created_at, ToolInvocation.id)))
    version = await db.get(AgentAssemblyVersion, work.agent_version_id)
    return {
        "case_key": f"run-{run.id}", "name": work.goal[:80],
        "input_text": work.goal, "weight": "1", "tags": [],
        # Observed failures/tools are evidence, never automatically gold labels.
        "assertions": {"min_chars": 1}, "source_run_id": run.id,
        "source_snapshot": {
            "run_id": run.id, "status": run.status,
            "agent_version_id": work.agent_version_id,
            "content_hash": version.content_hash if version else None,
            "input_text": work.goal, "response_text": response,
            "tools": [{"key": key, "status": status} for key, status in tools],
        },
    }


def compare_eval_results(baseline: dict, candidate: dict) -> dict:
    """Compare exact same immutable cases; changed tests are not a quality delta."""
    if baseline["agent_id"] != candidate["agent_id"]:
        raise ValueError("只能对比同一个智能体的评测")
    if baseline["suite_version_id"] != candidate["suite_version_id"]:
        raise ValueError("请选择同一评测集版本的两次运行，避免将用例变化误判为配置变化")
    if baseline["status"] not in {"passed", "failed", "error"} or candidate["status"] not in {"passed", "failed", "error"}:
        raise ValueError("评测结束后才能对比")
    before = {case["case_key"]: case for case in baseline["case_runs"]}
    after = {case["case_key"]: case for case in candidate["case_runs"]}
    if not before or set(before) != set(after):
        raise ValueError("评测用例不完整，无法对比")
    cases = []
    tags: dict[str, dict] = {}
    for key, new in after.items():
        old = before[key]
        old_pass, new_pass = old["status"] == "passed", new["status"] == "passed"
        change = "regressed" if old_pass and not new_pass else "improved" if new_pass and not old_pass else "unchanged"
        cases.append({
            "case_key": key, "name": new["name"], "change": change,
            "baseline_status": old["status"], "candidate_status": new["status"],
            "baseline_response": old.get("response_text"), "candidate_response": new.get("response_text"),
            "candidate_assertions": new["assertion_results"],
        })
        for tag in new.get("tags") or ["未分类"]:
            item = tags.setdefault(tag, {"tag": tag, "count": 0, "baseline_passed": 0, "candidate_passed": 0})
            item["count"] += 1
            item["baseline_passed"] += int(old_pass)
            item["candidate_passed"] += int(new_pass)
    return {
        "baseline_id": baseline["id"], "candidate_id": candidate["id"],
        "same_candidate": baseline["candidate_content_hash"] == candidate["candidate_content_hash"],
        "score_delta": str(Decimal(str(candidate["score"] or 0)) - Decimal(str(baseline["score"] or 0))),
        "regressed_count": sum(row["change"] == "regressed" for row in cases),
        "improved_count": sum(row["change"] == "improved" for row in cases),
        "cases": cases, "tags": list(tags.values()),
    }
