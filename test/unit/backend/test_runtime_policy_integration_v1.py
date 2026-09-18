"""Frozen Context, History and Memory policies feed the Runner prompt."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.artifact_v1 import Artifact
from app.models.common import utcnow, uuid7
from app.models.foundation import UserIdentity, UserSession
from app.models.memory_v1 import MemoryHead, MemoryRecord, RunnerMemorySnapshot
from app.models.registry import PolicyDefinition, PolicyVersion, RegisteredModel
from app.models.runtime_v1 import DurableRunner, Message, Run, RunnerCheckpoint, WorkItem
from app.runtime.context_assembler import ContextAssembler
from app.services.context_policy_service import CONTEXT_POLICY_DEFAULTS, LEGACY_CONTEXT_POLICY_DEFAULTS
from app.services.runtime_policy_service import (
    prepare_runtime_policy_inputs,
    resolve_frozen_runtime_policies,
)
from app.workers.runtime_worker import _model_history_context, _policy_prompt_sections


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
@pytest.mark.parametrize("context_key,context_overrides,artifact_count", [
    ("context.standard", {"token_budget": 8_000, "max_artifacts": 0}, 0),
    ("context.standard", CONTEXT_POLICY_DEFAULTS["context.standard"], 20),
    ("context.evidence", CONTEXT_POLICY_DEFAULTS["context.evidence"], 40),
    ("context.concise", {}, 5),
    ("context.evidence", {}, 40),
    ("context.evidence", {"max_artifacts": 3, "input_target_ratio": 0.65}, 3),
    ("context.concise", {"token_budget": 600, "max_artifacts": 40}, None),
])
async def test_frozen_policies_select_and_manifest_real_runner_inputs(
    context_key, context_overrides, artifact_count,
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    context = _policy("context", context_key, context_overrides)
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
    checkpoint_id = uuid7()
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
            RunnerCheckpoint(
                id=checkpoint_id, runner_id=runner_id, work_item_id=work_id,
                sequence_no=1, checkpoint_type="tool_results_persisted",
                state_snapshot={"private_body": "must-not-inject-checkpoint-body"},
                last_completed_step="verified_result", safe_to_resume=True,
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
        artifacts = []
        for index in range(45):
            artifact = Artifact(
                id=uuid7(), user_id="policy-user", session_id=session_id,
                workspace_id="metadata-only-workspace", relative_path=f"private/report-{index}.txt",
                name=f"report-{index}.txt", mime_type="text/plain", size_bytes=1_000_000,
                checksum=f"{index:064x}", publication_key=f"{index:064x}",
                created_by_agent_id="agent-1", status="published", version_no=1,
                created_at=now + timedelta(seconds=index),
            )
            artifacts.append(artifact)
            db.add(artifact)
        # Newer rows outside this Session/user or publication state must not
        # consume the selected profile's metadata quota.
        for index, overrides in enumerate((
            {"session_id": uuid7()}, {"user_id": "other-user"}, {"status": "unavailable"},
        ), 100):
            db.add(Artifact(
                id=uuid7(), workspace_id="hidden-workspace", relative_path=f"hidden-{index}",
                name="hidden evidence", mime_type="text/plain", size_bytes=5,
                checksum=f"{index:064x}", publication_key=f"{index:064x}",
                created_by_agent_id="agent-1", version_no=1,
                created_at=now + timedelta(seconds=index),
                **{"session_id": session_id, "user_id": "policy-user", "status": "published", **overrides},
            ))
        # The current registry head and mutable defaults deliberately disagree
        # with the Agent's frozen version; neither may change runtime inputs.
        new_context_version = PolicyVersion(
            id=uuid7(), policy_definition_id=context[0].id, version_no=4,
            implementation_ref=f"test:{context_key}:v4",
            parameters={"max_artifacts": 0, "input_target_ratio": 0.1},
            content_hash="z" * 64, status="published", published_at=utcnow(),
        )
        context[0].current_version_id = new_context_version.id
        db.add(new_context_version)
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
        assert selected.context.manifest["parameters"] == {
            **LEGACY_CONTEXT_POLICY_DEFAULTS[context_key], **context_overrides,
        }
        metadata = json.loads(selected.context.content)
        chosen_artifacts = metadata["published_artifacts"]
        if artifact_count is None:
            assert 0 < len(chosen_artifacts) < context_overrides["max_artifacts"]
        else:
            assert len(chosen_artifacts) == artifact_count
        assert [item["artifact_id"] for item in chosen_artifacts] == (
            [item.id for item in artifacts[-len(chosen_artifacts):]] if chosen_artifacts else []
        )
        assert selected.context.manifest["source_ids"]["artifact_ids"] == [
            item["artifact_id"] for item in chosen_artifacts
        ]
        assert selected.context.manifest["token_estimate"] <= selected.context.manifest["parameters"]["token_budget"]
        assert metadata["work_item"]["goal"] == "current request"
        assert metadata["execution_principal"]["current_agent_id"] == "agent-1"
        assert metadata["checkpoint"]["checkpoint_id"] == checkpoint_id
        assert metadata["checkpoint"]["last_completed_step"] == "verified_result"
        assert "must-not-inject-checkpoint-body" not in selected.context.content
        assert "hidden evidence" not in selected.context.content
        assert all(set(item) == {
            "artifact_id", "name", "mime_type", "checksum", "version_no", "parent_artifact_id",
        } for item in chosen_artifacts)
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
        history_messages, history_manifest = _model_history_context(selected.history.messages)
        package = ContextAssembler().assemble(
            platform_rules="platform",
            agent_instructions="agent",
            current_input="current request",
            history=history_messages,
            history_manifest=history_manifest,
            policy_sections=sections,
            web_result_budget_ratio=selected.context.manifest["parameters"]["web_result_budget_ratio"],
        )
        policy_messages = package.messages[1:1 + len(sections)]
        assert all(message["role"] == "assistant" for message in policy_messages)
        assert any("Traditional Chinese" in message["content"] for message in policy_messages)
        assert package.messages[-1] == {"role": "user", "content": "current request"}
        assert {item["source"] for item in package.manifest} >= {
            "context_policy", "memory_recall", "history", "current_input",
        }
        retained_history = [item for item in package.manifest if item["source"] == "history"]
        assert retained_history[0]["source_message_id"] == selected.history.messages[0].id
        assert package.web_result_budget_ratio == selected.context.manifest["parameters"]["web_result_budget_ratio"]

    await engine.dispose()
