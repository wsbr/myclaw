"""Frozen Context, History and Memory policies feed the Runner prompt."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.common import utcnow, uuid7
from app.models.foundation import UserIdentity, UserSession
from app.models.memory_v1 import MemoryHead, MemoryRecord, RunnerMemorySnapshot
from app.models.registry import PolicyDefinition, PolicyVersion, RegisteredModel
from app.models.runtime_v1 import DurableRunner, Message, Run, WorkItem
from app.runtime.context_assembler import ContextAssembler
from app.services.runtime_policy_service import (
    prepare_runtime_policy_inputs,
    resolve_frozen_runtime_policies,
)
from app.workers.runtime_worker import _model_history_messages, _policy_prompt_sections


pytestmark = pytest.mark.unit


def _policy(policy_type: str, key: str, parameters: dict):
    definition = PolicyDefinition(
        id=uuid7(),
        policy_key=key,
        policy_type=policy_type,
        name=key,
        description=key,
        implementation_ref=f"test:{key}",
        implementation_version="mutable-definition",
        default_parameters={"sentinel": "must-not-run"},
        parameter_version=99,
        enabled=True,
        status="active",
    )
    version = PolicyVersion(
        id=uuid7(),
        policy_definition_id=definition.id,
        version_no=3,
        implementation_ref=f"test:{key}:v3",
        parameters=parameters,
        content_hash=key[0] * 64,
        status="published",
        published_at=utcnow(),
    )
    definition.current_version_id = version.id
    return definition, version


@pytest.mark.asyncio
async def test_frozen_policies_select_and_manifest_real_runner_inputs():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    context = _policy("context", "context.standard", {
        "token_budget": 8_000, "max_artifacts": 0,
    })
    history = _policy("history", "history.recent", {
        "token_budget": 4_000, "max_messages": 1,
    })
    memory_write = _policy("memory_write", "memory_write.none", {})
    memory_recall = _policy("memory_recall", "memory_recall.user_combined", {
        "max_items": 1, "token_budget": 100,
    })
    policy_rows = [row for pair in (
        context, history, memory_write, memory_recall,
    ) for row in pair]
    assembly = SimpleNamespace(
        context_policy_id=context[0].id,
        context_policy_version_id=context[1].id,
        history_policy_id=history[0].id,
        history_policy_version_id=history[1].id,
        memory_write_policy_id=memory_write[0].id,
        memory_write_policy_version_id=memory_write[1].id,
        memory_recall_policy_id=memory_recall[0].id,
        memory_recall_policy_version_id=memory_recall[1].id,
    )

    session_id, run_id, work_id, runner_id = (uuid7() for _ in range(4))
    current_message_id = uuid7()
    now = utcnow()
    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="policy-user", status="active"),
            RegisteredModel(
                id="policy-model", key="policy-model", name="Policy model",
                provider="openai_compatible", model_name="policy-model",
                default_parameters={}, capabilities={}, enabled=True, status="active",
            ),
            *policy_rows,
            UserSession(
                id=session_id, user_id="policy-user", entry_agent_id="agent-1",
                status="active", origin_type="web", next_event_sequence=1,
            ),
            Run(
                id=run_id, session_id=session_id, user_id="policy-user",
                mode="chat", status="active",
            ),
            WorkItem(
                id=work_id, run_id=run_id, session_id=session_id,
                user_id="policy-user", mode="chat", owner_agent_id="agent-1",
                agent_version_id="agent-version-1", goal="current request",
                status="running", execution_epoch=1, attempt_no=1,
            ),
            DurableRunner(
                id=runner_id, work_item_id=work_id, run_id=run_id,
                session_id=session_id, agent_id="agent-1",
                agent_version_id="agent-version-1", execution_epoch=1,
                status="running", principal_snapshot={}, assembly_snapshot={},
                prompt_manifest={}, input_snapshot={"message_id": current_message_id},
            ),
            Message(
                id=uuid7(), session_id=session_id, user_id="policy-user",
                scope_type="chat", sender_type="agent", recipient_type="session",
                role="assistant", content="older history", status="committed",
                created_at=now - timedelta(minutes=2),
            ),
            Message(
                id=uuid7(), session_id=session_id, user_id="policy-user",
                scope_type="chat", sender_type="agent", recipient_type="session",
                role="assistant", content="selected recent history", status="committed",
                created_at=now - timedelta(minutes=1),
            ),
            Message(
                id=current_message_id, session_id=session_id, user_id="policy-user",
                scope_type="chat", sender_type="user", sender_user_id="policy-user",
                recipient_type="session", role="user", content="current request",
                status="committed", run_id=run_id, work_item_id=work_id,
                created_at=now,
            ),
        ])
        agent_head = MemoryHead(
            id=uuid7(), user_id="policy-user", scope="user_agent",
            agent_id="agent-1", fact_key="preferred_language",
        )
        user_head = MemoryHead(
            id=uuid7(), user_id="policy-user", scope="user",
            fact_key="timezone",
        )
        db.add_all([agent_head, user_head])
        await db.flush()
        db.add_all([
            MemoryRecord(
                id=uuid7(), head_id=agent_head.id, user_id="policy-user",
                agent_id="agent-1", scope="user_agent",
                fact_key="preferred_language", content="Traditional Chinese",
                normalized_hash="a" * 64, status="active", source_kind="import",
            ),
            MemoryRecord(
                id=uuid7(), head_id=user_head.id, user_id="policy-user",
                scope="user", fact_key="timezone", content="Asia/Taipei",
                normalized_hash="b" * 64, status="active", source_kind="import",
            ),
        ])
        await db.flush()

        frozen = await resolve_frozen_runtime_policies(db, assembly)
        runner = await db.get(DurableRunner, runner_id)
        run = await db.get(Run, run_id)
        selected = await prepare_runtime_policy_inputs(
            db,
            run=run,
            runner=runner,
            policies=frozen,
            model_id="policy-model",
            model_version_id="policy-model-version",
            exclude_message_ids={current_message_id},
        )

        assert selected.context.manifest["policy_version_id"] == context[1].id
        assert selected.context.manifest["parameters"] == context[1].parameters
        assert selected.history.manifest["policy_version_id"] == history[1].id
        assert selected.history.manifest["selected_message_ids"] == [
            selected.history.messages[0].id,
        ]
        assert selected.history.messages[0].content == "selected recent history"
        assert current_message_id not in selected.history.manifest["selected_message_ids"]
        assert [item.fact_key for item in selected.memories] == ["preferred_language"]
        assert selected.memory_manifest["policy_version_id"] == memory_recall[1].id
        snapshot = await db.get(
            RunnerMemorySnapshot,
            {"runner_id": runner_id, "memory_id": selected.memories[0].id},
        )
        assert snapshot is not None
        assert snapshot.content_hash == selected.memories[0].normalized_hash

        sections = _policy_prompt_sections(selected)
        package = ContextAssembler().assemble(
            platform_rules="platform",
            agent_instructions="agent",
            current_input="current request",
            history=_model_history_messages(selected.history.messages),
            policy_sections=sections,
        )
        policy_messages = package.messages[1:1 + len(sections)]
        assert all(message["role"] == "assistant" for message in policy_messages)
        assert any("Traditional Chinese" in message["content"] for message in policy_messages)
        assert package.messages[-1] == {"role": "user", "content": "current request"}
        assert {item["source"] for item in package.manifest} >= {
            "context_policy", "memory_recall", "history", "current_input",
        }

    await engine.dispose()
