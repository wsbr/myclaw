"""Release-gate decisions over exact Agent candidate and suite versions."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_eval_v1 import AgentEvalRun, AgentEvalSuite, AgentEvalSuiteVersion


async def agent_eval_gate_issues(
    db: AsyncSession, *, agent_id: str, candidate_content_hash: str,
    lock_suites: bool = False,
) -> list[str]:
    """Return unsatisfied active gates; an Agent without active suites is unchanged."""

    statement = select(AgentEvalSuite).where(
        AgentEvalSuite.agent_id == agent_id,
        AgentEvalSuite.status == "active",
    ).order_by(AgentEvalSuite.created_at, AgentEvalSuite.id)
    if lock_suites:
        statement = statement.with_for_update()
    suites = list(await db.scalars(statement))
    issues: list[str] = []
    for suite in suites:
        version = (
            await db.get(AgentEvalSuiteVersion, suite.current_version_id)
            if suite.current_version_id else None
        )
        if version is None:
            issues.append(f"eval suite {suite.name} has no immutable current version")
            continue
        passed = await db.scalar(select(AgentEvalRun.id).where(
            AgentEvalRun.agent_id == agent_id,
            AgentEvalRun.suite_version_id == version.id,
            AgentEvalRun.candidate_content_hash == candidate_content_hash,
            AgentEvalRun.status == "passed",
        ).order_by(AgentEvalRun.ended_at.desc(), AgentEvalRun.id.desc()).limit(1))
        if passed is None:
            issues.append(
                f"eval suite {suite.name} v{version.version_no} has not passed "
                f"candidate {candidate_content_hash[:12]}"
            )
    return issues
