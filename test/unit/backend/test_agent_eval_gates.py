from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import *  # noqa: F401,F403
from app.models.agent_eval_v1 import AgentEvalCase, AgentEvalCaseRun, AgentEvalRun
from app.models.agent_v1 import AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.foundation import PlatformAccount, UserIdentity, UserSession
from app.models.runtime_v1 import Message, Run
from app.models.usage_v1 import UsageLedgerEntry
from app.services.agent_eval_gate_service import agent_eval_gate_issues
from app.services.agent_eval_service import (
    create_eval_suite, create_eval_suite_version, evaluate_response,
    reconcile_one_eval_case,
)


pytestmark = pytest.mark.unit


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _cases(expected: str = "APPROVED") -> list[dict]:
    return [{
        "case_key": "review",
        "name": "Review outcome",
        "input_text": "Review this change",
        "assertions": {
            "contains_all": [expected],
            "contains_none": ["SECRET"],
            "min_chars": 8,
        },
        "weight": "2",
    }]


@pytest.mark.asyncio
async def test_current_suite_version_and_exact_candidate_hash_gate_publication():
    engine, factory = await _database()
    async with factory.begin() as db:
        user_id, account_id, agent_id = uuid7(), uuid7(), uuid7()
        db.add(UserIdentity(user_id=user_id, status="active"))
        db.add(PlatformAccount(
            id=account_id, user_id=user_id, display_name="eval-owner",
            status="active", auth_revision=1,
        ))
        db.add(AgentBlueprint(
            id=agent_id, owner_account_id=account_id, visibility="private",
            status="draft", next_version_no=1,
        ))
        await db.flush()
        suite, version = await create_eval_suite(
            db, agent_id=agent_id, actor_account_id=account_id,
            name="Release quality", description="Exact candidate gate",
            gate_policy={"min_pass_rate": "1"}, cases=_cases(),
        )
        candidate_hash = "a" * 64
        issues = await agent_eval_gate_issues(
            db, agent_id=agent_id, candidate_content_hash=candidate_hash,
        )
        assert len(issues) == 1
        db.add(AgentEvalRun(
            id=uuid7(), agent_id=agent_id, suite_version_id=version.id,
            candidate_agent_version_id=uuid7(), candidate_content_hash=candidate_hash,
            status="passed", case_count=1, passed_count=1, failed_count=0,
            error_count=0, score=Decimal(1), gate_policy_snapshot={"min_pass_rate": "1"},
            started_at=utcnow(), ended_at=utcnow(), created_by_account_id=account_id,
        ))
        await db.flush()
        assert await agent_eval_gate_issues(
            db, agent_id=agent_id, candidate_content_hash=candidate_hash,
        ) == []
        assert await agent_eval_gate_issues(
            db, agent_id=agent_id, candidate_content_hash="b" * 64,
        )

        newer = await create_eval_suite_version(
            db, suite=suite, actor_account_id=account_id, name="Release quality",
            description="Changed suite invalidates prior evidence",
            gate_policy={"min_pass_rate": "0.8"}, cases=_cases("VERIFIED"),
        )
        assert newer.version_no == 2
        assert suite.current_version_id == newer.id
        assert await agent_eval_gate_issues(
            db, agent_id=agent_id, candidate_content_hash=candidate_hash,
        )

        suite.status = "inactive"
        await db.flush()
        assert await agent_eval_gate_issues(
            db, agent_id=agent_id, candidate_content_hash="unrelated",
        ) == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_runtime_case_scores_response_and_unknown_cost_is_not_zero():
    engine, factory = await _database()
    async with factory.begin() as db:
        now = utcnow()
        user_id, account_id, agent_id = uuid7(), uuid7(), uuid7()
        session_id, runtime_run_id = uuid7(), uuid7()
        eval_run_id, case_id, suite_id, suite_version_id = (
            uuid7(), uuid7(), uuid7(), uuid7(),
        )
        db.add(UserIdentity(user_id=user_id, status="active"))
        db.add(PlatformAccount(
            id=account_id, user_id=user_id, display_name="eval-owner",
            status="active", auth_revision=1,
        ))
        db.add(UserSession(
            id=session_id, user_id=user_id, account_id=account_id,
            entry_agent_id=agent_id, status="active", origin_type="web",
            next_event_sequence=1, permission_mode="direct", permission_revision=1,
            interaction_mode="chat", interaction_revision=1,
        ))
        db.add(Run(
            id=runtime_run_id, session_id=session_id, user_id=user_id,
            mode="chat", status="completed", permission_mode="direct",
            hard_budget_snapshot={}, completed_at=now,
        ))
        response = Message(
            id=uuid7(), session_id=session_id, user_id=user_id, scope_type="chat",
            sender_type="agent", sender_agent_id=agent_id,
            recipient_type="session", role="assistant",
            content="Review APPROVED with evidence.", content_format="markdown",
            status="committed", run_id=runtime_run_id,
        )
        db.add(response)
        db.add(AgentEvalCase(
            id=case_id, suite_version_id=suite_version_id, case_key="review",
            name="Review", input_text="Review it",
            assertions={"contains_all": ["APPROVED"], "min_chars": 10},
            weight=Decimal(1), sort_order=0,
        ))
        db.add(AgentEvalRun(
            id=eval_run_id, agent_id=agent_id, suite_version_id=suite_version_id,
            candidate_agent_version_id=uuid7(), candidate_content_hash="c" * 64,
            status="queued", case_count=1, passed_count=0, failed_count=0,
            error_count=0, gate_policy_snapshot={"min_pass_rate": "1"},
            created_by_account_id=account_id,
        ))
        case_run = AgentEvalCaseRun(
            id=uuid7(), eval_run_id=eval_run_id, eval_case_id=case_id,
            session_id=session_id, run_id=runtime_run_id, status="queued",
            assertion_results=[],
        )
        db.add(case_run)
        db.add(UsageLedgerEntry(
            id=uuid7(), account_id=account_id, session_id=session_id,
            run_id=runtime_run_id, source_type="adjustment", source_id=uuid7(),
            source_operation_key=f"unknown:{uuid7()}", metric="requests",
            unit="request", quantity=Decimal(1), measurement_status="measured",
            cost_status="unknown", safe_metadata={}, created_at=now,
        ))
        await db.flush()

        cost_results = await evaluate_response(
            db, run_id=runtime_run_id, response=response.content,
            assertions={
                "contains_all": ["APPROVED"],
                "max_cost": {"currency": "USD", "amount": "1", "allow_unknown": False},
            },
        )
        assert cost_results[0]["passed"] is True
        assert cost_results[1]["passed"] is False
        assert cost_results[1]["unknown_entry_count"] == 1

        assert await reconcile_one_eval_case(db) is True
        assert case_run.status == "passed"
        eval_run = await db.get(AgentEvalRun, eval_run_id)
        assert eval_run.status == "passed"
        assert eval_run.score == Decimal(1)
        assert eval_run.ended_at is not None
    await engine.dispose()
