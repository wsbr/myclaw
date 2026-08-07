"""capacity_risk.v1 consumes real process and immutable file facts."""

from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.tool_registry.base import RuntimeContext
from app.core.tool_registry.registry import ToolRegistry
from app.database import Base
from app.models.agent_v1 import AgentAssemblyVersion, AgentBlueprint
from app.models.artifact_v1 import Artifact
from app.models.common import utcnow, uuid7
from app.models.foundation import PlatformAccount, UserIdentity, UserSession
from app.models.registry import PlatformTool, SandboxProfile
from app.models.runtime_v1 import (
    DurableRunner, Run, SessionSandbox, ToolAttempt, ToolInvocation, WorkItem,
    Workspace,
)
from app.models.verification_v1 import VerificationEvidenceRef, VerificationResult
from app.services.verification_service import (
    CAPACITY_ARTIFACT_NAMES, CAPACITY_CASES, verify_capacity_risk,
)


pytestmark = pytest.mark.unit


CAPACITY_PROGRAM = '''from __future__ import annotations
import argparse
import json

def emit(value, code=0):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    raise SystemExit(code)

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--capacity", type=float)
parser.add_argument("--committed", type=float)
args = parser.parse_args()
if args.committed is None:
    emit({"error": {"code": "missing_committed", "message": "--committed is required"}}, 2)
if args.capacity is None or args.capacity <= 0:
    emit({"error": {"code": "invalid_capacity", "message": "total_capacity_hours must be greater than 0"}}, 2)
if args.committed < 0:
    emit({"error": {"code": "invalid_committed", "message": "committed_hours must be greater than or equal to 0"}}, 2)
capacity = int(args.capacity) if args.capacity.is_integer() else args.capacity
committed = int(args.committed) if args.committed.is_integer() else args.committed
utilization = round(committed / capacity * 100, 2)
risk = "low" if utilization < 75 else "medium" if utilization <= 90 else "high"
emit({
    "committed_hours": committed,
    "over_capacity_hours": max(committed - capacity, 0),
    "risk": risk,
    "total_capacity_hours": capacity,
    "utilization_percent": utilization,
})
'''


ARTIFACT_CONTENT = {
    "requirements.md": """# Capacity risk requirements

utilization_percent = committed_hours / total_capacity_hours * 100
low: utilization < 75
medium: 75 <= utilization <= 90
high: utilization > 90
over_capacity_hours = max(committed_hours - total_capacity_hours, 0)
total_capacity_hours > 0
committed_hours >= 0
Cases: T01 T02 T03 T04 T05 T06 T07 T08 T09
""",
    "capacity_risk.py": CAPACITY_PROGRAM,
    "test_report.md": """# Test report

Result: 9/9. For T01 T02 T03 T04 T05 T06 T07 T08 T09 the exact argv,
stdout, stderr, and exit code were recorded from ToolAttempt facts.
""",
    "acceptance_report.md": """# Acceptance report

capacity_risk.v1: accepted, 9/9.
Artifacts: requirements.md, capacity_risk.py, test_report.md, acceptance_report.md.
""",
}


async def _seed_world(factory, tmp_path):
    ids = {key: uuid7() for key in (
        "account", "agent", "version", "profile", "execute_tool", "publish_tool",
        "session", "run", "work", "runner", "sandbox", "workspace",
    )}
    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="capacity-user", status="active"),
            PlatformAccount(
                id=ids["account"], user_id="capacity-user", display_name="Capacity User",
                status="active",
            ),
            SandboxProfile(
                id=ids["profile"], profile_key="capacity-verification", type="local",
                description="test", implementation_ref="test", config={},
                config_version=1, enabled=True,
            ),
            PlatformTool(
                id=ids["execute_tool"], tool_key="execute", display_name="Execute",
                description="test", input_schema={}, implementation_ref="test",
                implementation_version="1", execution_mode="durable_async", enabled=True,
                confirmation_policy="session_grant", side_effect="non_idempotent",
                platform_config={"workspace_access": "write"}, config_version=1,
                registration_hash="a" * 64,
            ),
            PlatformTool(
                id=ids["publish_tool"], tool_key="publish_artifact", display_name="Publish",
                description="test", input_schema={}, implementation_ref="test",
                implementation_version="1", execution_mode="inline", enabled=True,
                confirmation_policy="session_grant", side_effect="idempotent",
                platform_config={"workspace_access": "write"}, config_version=1,
                registration_hash="b" * 64,
            ),
            AgentBlueprint(
                id=ids["agent"], owner_account_id=ids["account"], visibility="enterprise",
                status="published", current_published_version_id=ids["version"],
                draft_version_id=uuid7(), next_version_no=2,
            ),
            AgentAssemblyVersion(
                id=ids["version"], agent_id=ids["agent"], version_no=1,
                publication_status="published", name="Capacity Agent", description="test",
                instructions="test", group_enabled=True, created_by_account_id=ids["account"],
                immutable_at=utcnow(), published_at=utcnow(), content_hash="c" * 64,
            ),
            UserSession(
                id=ids["session"], user_id="capacity-user", entry_agent_id=ids["agent"],
                account_id=ids["account"], status="active", origin_type="web",
                next_event_sequence=1,
            ),
            Run(
                id=ids["run"], session_id=ids["session"], user_id="capacity-user",
                mode="chat", status="completed", terminal_reason="completed",
                completed_at=utcnow(),
            ),
            WorkItem(
                id=ids["work"], run_id=ids["run"], session_id=ids["session"],
                user_id="capacity-user", mode="chat", owner_agent_id=ids["agent"],
                agent_version_id=ids["version"], goal="capacity", status="succeeded",
                execution_epoch=1, attempt_no=1,
            ),
            DurableRunner(
                id=ids["runner"], work_item_id=ids["work"], run_id=ids["run"],
                session_id=ids["session"], agent_id=ids["agent"],
                agent_version_id=ids["version"], execution_epoch=1, status="succeeded",
                principal_snapshot={}, assembly_snapshot={}, prompt_manifest={}, input_snapshot={},
            ),
            SessionSandbox(
                id=ids["sandbox"], session_id=ids["session"],
                sandbox_profile_id=ids["profile"], profile_config_version=1,
                status="ready", root_uri="workspace://capacity/",
            ),
            Workspace(
                id=ids["workspace"], session_sandbox_id=ids["sandbox"],
                session_id=ids["session"], root_uri="workspace://capacity/",
                status="active", retention_policy_key="session_lifetime",
            ),
        ])
    root = tmp_path / ids["session"]
    private = root / "agents" / ids["agent"]
    private.mkdir(parents=True)
    (private / "capacity_risk.py").write_text(CAPACITY_PROGRAM)
    return ids, root, private


@pytest.mark.asyncio
async def test_capacity_risk_v1_requires_real_nine_of_nine_and_four_exact_artifacts(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(settings, "sandbox_root", str(tmp_path))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ids, root, _private = await _seed_world(factory, tmp_path)
    context = RuntimeContext(
        session_id=ids["session"], runner_id=ids["runner"], task_id=ids["work"],
        user_id="capacity-user", account_id=ids["account"], agent_id=ids["agent"],
        workspace_id=ids["workspace"], execution_epoch=1,
    )

    for attempt_no, case in enumerate(CAPACITY_CASES, start=1):
        invocation_id = uuid7()
        arguments = {"argv": list(case.argv), "cwd": ".", "timeout_seconds": 10}
        encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        async with factory.begin() as db:
            db.add(ToolInvocation(
                id=invocation_id, runner_id=ids["runner"], work_item_id=ids["work"],
                run_id=ids["run"], session_id=ids["session"],
                platform_tool_id=ids["execute_tool"], tool_key="execute",
                execution_mode="durable_async", status="running", arguments=arguments,
                arguments_hash=hashlib.sha256(encoded.encode()).hexdigest(),
                business_idempotency_key=f"capacity:{case.case_id}",
                principal_hash="d" * 64, attempt_count=1, execution_epoch=1,
            ))
        async with factory() as db:
            result = await ToolRegistry.execute("execute", arguments, context, db=db)
        assert result.success is True
        assert result.data["exit_code"] == case.exit_code
        assert result.data["stdout"] == case.stdout
        assert result.data["stderr"] == ""
        snapshot = result.model_dump(mode="json")
        request = {
            "tool_key": "execute", "operation_key": f"capacity:{case.case_id}",
            "arguments_hash": hashlib.sha256(encoded.encode()).hexdigest(),
            "arguments": arguments, "argv": list(case.argv), "cwd": ".",
            "execution_epoch": 1,
        }
        async with factory.begin() as db:
            invocation = await db.get(ToolInvocation, invocation_id)
            invocation.status = "succeeded"
            invocation.result_data = snapshot
            db.add(ToolAttempt(
                id=uuid7(), tool_invocation_id=invocation_id, attempt_no=attempt_no,
                status="succeeded", worker_id="real-process-test", lease_token=uuid7(),
                request_snapshot=request,
                request_hash=hashlib.sha256(json.dumps(
                    request, sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest(),
                response_snapshot=snapshot, execution_epoch=1,
                fencing_token=attempt_no, side_effect_started=True,
                outcome_confirmed=True, started_at=utcnow(), ended_at=utcnow(),
            ))

    for name in CAPACITY_ARTIFACT_NAMES:
        artifact_id = uuid7()
        content = ARTIFACT_CONTENT[name].encode()
        target = root / ".myclaw" / "artifacts" / artifact_id / name
        target.parent.mkdir(parents=True)
        target.write_bytes(content)
        async with factory.begin() as db:
            db.add(Artifact(
                id=artifact_id, user_id="capacity-user", session_id=ids["session"],
                workspace_id=ids["workspace"],
                relative_path=f".myclaw/artifacts/{artifact_id}/{name}",
                source_relative_path=name, name=name,
                mime_type="text/markdown" if name.endswith(".md") else "text/x-python",
                size_bytes=len(content), checksum=hashlib.sha256(content).hexdigest(),
                created_by_agent_id=ids["agent"], work_item_id=ids["work"],
                runner_id=ids["runner"], status="published", version_no=1,
            ))

    async with factory.begin() as db:
        passed = await verify_capacity_risk(db, run_id=ids["run"], actor_id="unit-verifier")
        assert passed.status == "passed"
        assert len(passed.observed_facts) == 13
        replay = await verify_capacity_risk(
            db, run_id=ids["run"], actor_id="unit-verifier",
        )
        assert replay.id == passed.id and replay.verification_epoch == 1
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(VerificationResult)) == 1
        assert await db.scalar(select(func.count()).select_from(VerificationEvidenceRef)) == 13
        attempts = list(await db.scalars(select(ToolAttempt).order_by(ToolAttempt.attempt_no)))
        assert [item.request_snapshot["argv"] for item in attempts] == [
            list(case.argv) for case in CAPACITY_CASES
        ]

    acceptance_path = next(root.glob(".myclaw/artifacts/*/acceptance_report.md"))
    acceptance_path.write_text("corrupted after publication")
    async with factory.begin() as db:
        failed = await verify_capacity_risk(db, run_id=ids["run"], actor_id="unit-verifier")
        assert failed.status == "failed"
        assert failed.verification_epoch == 2
        assert any(
            item.get("fact") == "artifact_mismatch" for item in failed.missing_facts
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_capacity_risk_v1_reports_insufficient_authoritative_evidence(
    tmp_path, monkeypatch,
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(settings, "sandbox_root", str(tmp_path))
    ids, _root, _private = await _seed_world(factory, tmp_path)
    async with factory.begin() as db:
        result = await verify_capacity_risk(
            db, run_id=ids["run"], actor_id="unit-verifier",
        )
        assert result.status == "insufficient_evidence"
        assert any(
            item.get("fact") == "attempt_cardinality"
            and item.get("observed") == 0
            for item in result.missing_facts
        )
        assert any(
            item.get("fact") == "artifact_cardinality"
            and item.get("observed") == 0
            for item in result.missing_facts
        )
    await engine.dispose()
