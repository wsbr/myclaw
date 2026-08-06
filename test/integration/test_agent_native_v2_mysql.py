"""MySQL 8 acceptance for immutable publication and the unified Runtime path."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.model_hub.base import BaseLLM, LLMChunk, LLMResponse, TokenUsage, ToolCall
from app.core.tool_registry.base import RuntimeContext, ToolResult
from app.models.agent_v1 import AgentAssemblyVersion, AgentCard
from app.models.common import utcnow, uuid7
from app.models.foundation import PlatformAccount, UserIdentity, UserSession
from app.models.registry import (
    ConformanceRun, ModelVersion, PlatformTool, PolicyDefinition,
    RegisteredSkill, SandboxProfile, SkillVersion,
)
from app.models.runtime_v1 import DurableRunner, Run, WorkItem
from app.persistence.runtime_graph import persist_new_work_runner
from app.runtime import (
    ActionDispatcher, AgentLoopCoordinator, ContextAssembler, LoopIdentity,
    ModelAdapter, SnapshotResolver,
)
from app.services.agent_assembly_service import (
    AssemblySelection, create_agent, create_draft_test_snapshot,
    publish_agent, update_agent_draft,
)
from app.services.model_registry_service import (
    activate_model_version, create_logical_model, create_model_version,
)
from app.services.registry_reconcile import seed_core_registries

pytestmark = pytest.mark.integration


class _NativeProvider(BaseLLM):
    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.calls = 0
        self.requests = []

    async def chat(self, messages, tools, params) -> LLMResponse:
        raise NotImplementedError

    async def stream(self, messages, tools, params) -> AsyncIterator[LLMChunk]:
        self.calls += 1
        self.requests.append({"messages": messages, "tools": tools})
        if self.calls == 1:
            yield LLMChunk(type="tool_call_delta", tool_call=ToolCall(
                id="mysql-call", name=self.tool_name, arguments={"key": "alpha"},
            ))
            yield LLMChunk(type="done", stop_reason="tool_calls", usage=TokenUsage(
                input_tokens=10, output_tokens=2,
            ))
        else:
            yield LLMChunk(type="text_delta", content="MySQL unified Runtime passed.")
            yield LLMChunk(type="done", stop_reason="stop", usage=TokenUsage(
                input_tokens=12, output_tokens=4,
            ))

    async def count_tokens(self, messages):
        return 1


@pytest.mark.asyncio
async def test_mysql_publication_snapshot_and_native_runtime_round_trip():
    database_url = os.getenv("MYCLAW_V2_ACCEPTANCE_DATABASE_URL")
    if not database_url:
        pytest.skip("MYCLAW_V2_ACCEPTANCE_DATABASE_URL selects the isolated migrated database")
    engine = create_async_engine(database_url, isolation_level="READ COMMITTED")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid7()
    account_id = uuid7()

    async with factory.begin() as db:
        db.add(UserIdentity(user_id=f"v2-user-{suffix}", status="active"))
        await db.flush()
        db.add(PlatformAccount(
            id=account_id, user_id=f"v2-user-{suffix}",
            display_name="V2 MySQL Acceptance", status="active",
        ))
        await db.flush()
        await seed_core_registries(db)
        model = await create_logical_model(
            db, key=f"v2.mysql.{suffix}", name="V2 MySQL",
            actor_account_id=account_id,
        )
        model_version = await create_model_version(
            db, model_id=model.id, expected_version=1,
            actor_account_id=account_id, values={
                "provider": "openai_compatible", "model_name": "mysql-native",
                "endpoint": "http://provider.invalid/v1", "credential_ref": None,
                "default_parameters": {"temperature": 0},
                "capabilities": {
                    "streaming": True, "native_tool_calling": True,
                    "tool_result_continuation": True, "stop_reason": True,
                    "context_window": 8192,
                },
            },
        )
        db.add(ConformanceRun(
            subject_type="model_version", subject_id=model_version.id,
            suite_version=settings.provider_conformance_suite_version,
            idempotency_key=f"mysql-pass-{suffix}", request_hash="1" * 64,
            status="passed", started_at=utcnow(), ended_at=utcnow(),
            result_summary={"passed": True}, content_hash="2" * 64,
        ))
        await db.flush()
        await activate_model_version(
            db, model_id=model.id, model_version_id=model_version.id,
            expected_version=2,
        )

        tool = PlatformTool(
            id=uuid7(), tool_key=f"mysql_lookup_{suffix}",
            display_name="MySQL Lookup", description="Lookup a value",
            input_schema={
                "type": "object", "properties": {"key": {"type": "string"}},
                "required": ["key"], "additionalProperties": False,
            }, output_schema={"type": "object"}, implementation_ref="tests:mysql_lookup",
            implementation_version="1", execution_mode="inline", side_effect="none",
            supports_cancel=False, supports_status_lookup=False,
            enabled=True, confirmation_policy="never", platform_config={},
            config_version=1, registration_hash="3" * 64,
        )
        db.add(tool)
        skill = RegisteredSkill(
            id=uuid7(), logical_name=f"mysql.skill.{suffix}", name="MySQL Skill",
            description="Acceptance skill", owner_account_id=account_id,
            status="active",
        )
        db.add(skill)
        await db.flush()
        skill_version = SkillVersion(
            id=uuid7(), skill_id=skill.id, version_no=1, status="published",
            package_uri=f"memory://mysql.skill/{suffix}", checksum="4" * 64,
            manifest={"name": "mysql.skill", "instructions": "Use lookup safely."},
            content_hash="5" * 64, manifest_name="MySQL Skill",
            manifest_description="Acceptance skill", created_by_account_id=account_id,
            published_at=utcnow(),
        )
        db.add(skill_version)
        await db.flush()
        skill.current_published_version_id = skill_version.id

        policies = {row.policy_key: row.id for row in await db.scalars(select(PolicyDefinition))}
        sandbox_id = await db.scalar(select(SandboxProfile.id).where(
            SandboxProfile.profile_key == "local"
        ))
        agent, draft = await create_agent(
            db, owner_account_id=account_id, created_by_account_id=account_id,
            name="MySQL Agent", description="MySQL acceptance Agent",
        )
        draft = await update_agent_draft(
            db, agent_id=agent.id, expected_draft_revision=draft.draft_revision,
            values={
                "instructions": "Use the provided native Tool.", "model_id": model.id,
                "sandbox_profile_id": sandbox_id,
                "context_policy_id": policies["context.standard"],
                "history_policy_id": policies["history.recent"],
                "memory_write_policy_id": policies["memory_write.none"],
                "memory_recall_policy_id": policies["memory_recall.none"],
            }, selection=AssemblySelection(tool_ids=(tool.id,), skill_ids=(skill.id,)),
        )
        published = await publish_agent(
            db, agent_id=agent.id, expected_draft_revision=draft.draft_revision,
            actor_account_id=account_id, idempotency_record_id=f"publish-{suffix}",
        )
        draft_test = await create_draft_test_snapshot(
            db, agent_id=agent.id, expected_draft_revision=draft.draft_revision,
            actor_account_id=account_id, idempotency_record_id=f"draft-test-{suffix}",
        )
        assert draft_test.publication_status == "draft_test_snapshot"
        assert await db.scalar(select(AgentCard.id).where(
            AgentCard.agent_version_id == published.id
        ))

        session = UserSession(
            id=uuid7(), user_id=f"v2-user-{suffix}", account_id=account_id,
            entry_agent_id=agent.id, status="active", origin_type="web",
            permission_mode="standard", permission_revision=1,
        )
        run = Run(
            id=uuid7(), session_id=session.id, user_id=session.user_id,
            mode="chat", execution_kind="normal", lifecycle="running",
            status="active", entry_agent_id=agent.id, permission_mode="standard",
            hard_budget_snapshot={}, started_at=utcnow(),
        )
        work = WorkItem(
            id=uuid7(), run_id=run.id, session_id=session.id, user_id=session.user_id,
            mode="chat", owner_agent_id=agent.id, agent_version_id=published.id,
            goal="find alpha", purpose="root_request", status="queued",
            resume_cursor={}, execution_epoch=1, attempt_no=1,
        )
        runner = DurableRunner(
            id=uuid7(), work_item_id=work.id, run_id=run.id, session_id=session.id,
            agent_id=agent.id, agent_version_id=published.id, execution_epoch=1,
            fencing_token=1, status="running",
            principal_snapshot={"user_id": session.user_id, "account_id": account_id},
            assembly_snapshot={}, prompt_manifest={}, input_snapshot={},
        )
        db.add_all([session, run])
        await db.flush()
        await persist_new_work_runner(db, work, runner)
        resolved = await SnapshotResolver().resolve(
            db, runner=runner, execution_principal=runner.principal_snapshot,
            permission_mode="standard", hard_budget={},
        )
        assert resolved.assembly.id == published.id

        provider = _NativeProvider(tool.tool_key)

        async def execute(name, arguments, context, dispatch_db):
            return ToolResult(status="succeeded", summary="Found", data={"value": 42})

        result = await AgentLoopCoordinator(
            ModelAdapter(provider),
            ActionDispatcher(list(resolved.tool_definitions), executor=execute),
        ).run(
            context=ContextAssembler().assemble(
                platform_rules="Use native Tools.",
                agent_instructions=published.instructions, current_input=work.goal,
            ), tool_definitions=list(resolved.tool_definitions),
            capability_snapshot=resolved.capability,
            runtime_context=RuntimeContext(
                session_id=session.id, runner_id=runner.id, task_id=work.id,
                user_id=session.user_id, agent_id=agent.id,
            ), model_parameters={"model_id": model_version.model_name}, db=db,
            identity=LoopIdentity(
                runner_id=runner.id, work_item_id=work.id, run_id=run.id,
                agent_version_id=published.id, model_version_id=model_version.id,
                provider=model_version.provider,
                execution_epoch=runner.execution_epoch,
            ),
        )
        assert result.final_message == "MySQL unified Runtime passed."
        assert provider.requests[0]["tools"] == provider.requests[1]["tools"]

    async with factory() as db:
        with pytest.raises(DBAPIError):
            await db.execute(text(
                "UPDATE agent_version SET name='tampered' WHERE id=:version_id"
            ), {"version_id": published.id})
            await db.commit()
        await db.rollback()
        stored = await db.get(AgentAssemblyVersion, published.id)
        assert stored.name == "MySQL Agent"
        with pytest.raises(DBAPIError):
            await db.execute(text(
                "UPDATE model_version SET model_name='tampered' WHERE id=:version_id"
            ), {"version_id": model_version.id})
            await db.commit()
        await db.rollback()

    await engine.dispose()
