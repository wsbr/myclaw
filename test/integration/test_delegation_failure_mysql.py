"""Real MySQL evidence delivery and ProviderAttempt metadata; no external I/O."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select, text

from app.core.model_hub.base import BaseLLM, LLMChunk
from app.core.tool_registry.base import ToolResult
from app.models.common import utcnow, uuid7
from app.models.registry import ToolConnection, ToolConnectionRevision, ToolDefinition, ToolGovernance
from app.models.runtime_v1 import (
    AgentInvocation, AgentTurn, DurableRunner, Message, ProviderAttempt,
    Run, ToolInvocation, WorkItem,
)
from app.runtime.agent_loop_coordinator import AgentLoopCoordinator, AgentLoopError, LoopIdentity
from app.runtime.context_assembler import ContextPackage
from app.runtime.model_adapter import ModelAdapter
from app.workers.runtime_worker import RuntimeWorker
from test.integration.test_agent_collaboration_mysql import (
    invoke, invoked_probe, mysql_collaboration, publish, root_probe,
)

pytestmark = pytest.mark.integration


async def seed_completed_search(db, probe, *, url, status="succeeded"):
    definition = next(item for item in probe.resolved.capability.authorized_tool_versions
                      if item["name"] == "web_search")
    result = ToolResult(
        status=status, summary="Retrieved one source" if status == "succeeded" else "Search failed",
        data={"query": "local fixture", "retrieved_at": "2026-09-23T00:00:00Z", "results": [{
            "url": url, "title": "Published product announcement",
            "snippet": "The source reports a product announcement.", "source_id": "source-local",
        }]},
    )
    row = ToolInvocation(
        id=uuid7(), tenant_id=probe.run.tenant_id, account_id=probe.run.account_id,
        runner_id=probe.runner.id, work_item_id=probe.work.id, run_id=probe.run.id,
        session_id=probe.run.session_id, platform_tool_id=definition["platform_tool_id"],
        tool_version_id=definition["tool_version_id"],
        tool_version_content_hash=definition["tool_version_content_hash"],
        tool_key="web_search", source_turn_id=probe.turn.id,
        capability_snapshot_id=probe.resolved.capability.id,
        execution_mode=definition["execution_mode"], status=status,
        arguments={"query": "local fixture"}, arguments_hash="a" * 64,
        business_idempotency_key=uuid7(), principal_hash="b" * 64,
        result_data=result.model_dump(mode="json"), attempt_count=1,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.mark.parametrize("mode", ["chat", "plan"])
async def test_mysql_failed_child_delivers_only_its_completed_search_to_resumed_caller(mysql_collaboration, mode):
    f = mysql_collaboration
    async with f.factory() as db:
        catalog = {row.tool_key: row for row in await db.scalars(select(ToolDefinition).where(
            ToolDefinition.tool_key.in_(["web_search", "web_fetch"]),
        ))}
        # A frozen connection is required by real Agent publication. The endpoint
        # is deliberately non-routable and no tool/provider network call occurs;
        # completed retrieval records below are local integration fixtures.
        connection = await db.scalar(select(ToolConnection).where(ToolConnection.is_primary.is_(True)))
        if connection is None:
            connection = ToolConnection(id=uuid7(), connection_key="local-delegation-search",
                                        display_name="Local fixture", connection_type="bailian",
                                        auth_type="bearer", status="active", is_primary=True)
            db.add(connection)
            await db.flush()
            revision = ToolConnectionRevision(id=uuid7(), connection_id=connection.id, revision_no=1,
                                              endpoint="https://no-provider.invalid/search", auth_type="bearer",
                                              content_hash="c" * 64)
            db.add(revision)
            await db.flush()
            connection.current_revision_id = revision.id
        for tool in catalog.values():
            governance = await db.get(ToolGovernance, tool.id)
            governance.enabled = True
            governance.confirmation_policy = "never"
        search_fixture = SimpleNamespace(**{**vars(f), "tool_ids": tuple(row.id for row in catalog.values())})
        child = await publish(db, search_fixture, "Evidence collector")
        parent = await publish(db, f, "Caller", bound=(child.agent.id,))
        caller = await root_probe(db, f, parent, mode=mode)
        dispatched = await invoke(db, caller, child.agent.id, call_id="delegated-search")
        target, invocation = await invoked_probe(db, dispatched)
        saved = await seed_completed_search(db, target, url="https://example.com/completed")
        await seed_completed_search(db, target, url="https://example.com/failed", status="failed")
        unrelated = await root_probe(db, search_fixture, child, mode=mode)
        await seed_completed_search(db, unrelated, url="https://example.com/unrelated")
        caller.work.status = "waiting_agent"
        caller.work.wait_reason = "waiting_agent"
        caller.work.active_runner_id = None
        caller.runner.status = "succeeded"
        caller.runner.ended_at = utcnow()
        caller.run.status = "waiting_agent"
        caller.run.wait_reason = "waiting_agent"
        ids = (invocation.id, target.runner.id, target.work.id, caller.run.id, caller.work.id, saved.id)
        await db.commit()

    # Settlement reads previously committed evidence across transactions.
    async with f.factory() as db:
        invocation = await db.get(AgentInvocation, ids[0])
        runner = await db.get(DurableRunner, ids[1])
        work = await db.get(WorkItem, ids[2])
        run = await db.get(Run, ids[3])
        await RuntimeWorker(worker_id="local-integration")._settle_agent_target(
            db, runner=runner, work=work, run=run, invocation=invocation,
            error=AgentLoopError("Provider call failed: provider_transport_error", code="provider_transport_error"),
        )
        await db.commit()

    async with f.factory() as db:
        invocation = await db.get(AgentInvocation, ids[0])
        assert invocation.status == "failed"
        assert invocation.error_code == "agent_invocation_failed"
        partial = invocation.result_data["partial_results"]
        assert partial["status"] == "partial" and partial["completed"] is False
        assert partial["sources"] and partial["sources"][0]["tool_invocation_id"] == ids[5]
        serialized = json.dumps(partial)
        assert "https://example.com/completed" in serialized
        assert "https://example.com/failed" not in serialized
        assert "https://example.com/unrelated" not in serialized
        caller_work = await db.get(WorkItem, ids[4])
        resumed = await db.get(DurableRunner, caller_work.active_runner_id)
        assert resumed.status == "queued" and resumed.execution_epoch == 2
        tool_messages = [item for item in resumed.input_snapshot["pending_results"] if item["role"] == "tool"]
        delivered = json.loads(tool_messages[-1]["content"])
        assert delivered["status"] == "failed"
        assert delivered["data"]["partial_results"] == partial
        assert await db.scalar(select(func.count()).select_from(ToolInvocation).where(
            ToolInvocation.run_id == ids[3], ToolInvocation.tool_key == "web_search",
        )) == 2  # No tool replay or fabricated success.
        assert await db.scalar(text("SELECT @@foreign_key_checks")) == 1
        notice = await db.get(Message, invocation.result_message_id)
        assert "failed" in notice.content.lower()
        assert "部分成果（任务失败，尚未完成）" in notice.content
        assert "Published product announcement" in notice.content
        assert "https://example.com/completed" in notice.content
        assert "The source reports a product announcement." in notice.content
        assert "检索时间：" in notice.content and "完成时间：" in notice.content
        assert ids[5] not in notice.content
        assert "tool_invocation_id" not in notice.content and "tool_version_id" not in notice.content


class MetadataThenFailure(BaseLLM):
    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = 0
        self.request_id = "req-mysql-" + uuid7()

    async def chat(self, messages, tools, params):
        raise AssertionError("No nonstreaming request expected")

    async def count_tokens(self, messages, tools=None, params=None):
        return 1

    async def stream(self, messages, tools, params):
        self.calls += 1
        yield LLMChunk(type="metadata", provider_request_id=self.request_id,
                       provider_request_id_source="header:x-request-id", provider_response_id="message-local")
        async with self.fixture.factory() as reader:
            attempt = await reader.scalar(select(ProviderAttempt).where(
                ProviderAttempt.provider_request_id == self.request_id,
            ))
            assert attempt is not None and attempt.status == "running"
            assert not attempt.received_payload and attempt.first_payload_at is None
        yield LLMChunk(type="text_delta", content="Incomplete internal response")
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body (incomplete chunked read)")


async def test_mysql_request_id_commits_before_payload_and_survives_disconnect(mysql_collaboration):
    f = mysql_collaboration
    async with f.factory() as db:
        agent = await publish(db, f, "Metadata verification")
        probe = await root_probe(db, f, agent)
        await db.commit()
        provider = MetadataThenFailure(f)
        with pytest.raises(AgentLoopError) as caught:
            await AgentLoopCoordinator(ModelAdapter(provider), probe.dispatcher).run(
                context=ContextPackage(({"role": "user", "content": "Local failure verification"},), (), 1),
                tool_definitions=list(probe.resolved.tool_definitions),
                capability_snapshot=probe.resolved.capability, runtime_context=probe.context,
                model_parameters={}, db=db,
                identity=LoopIdentity(probe.runner.id, probe.work.id, probe.run.id,
                                      agent.version.id, agent.version.model_version_id, "openai"),
            )
        assert caught.value.code == "provider_transport_error"
        assert provider.calls == 1  # The deferred partial-payload retry policy stays unchanged.
        await db.commit()
        run_id, request_id = probe.run.id, provider.request_id
    async with f.factory() as reader:
        attempt = await reader.scalar(select(ProviderAttempt).where(
            ProviderAttempt.provider_request_id == request_id,
        ))
        assert attempt.status == "outcome_unknown" and attempt.received_payload
        assert attempt.first_payload_at is not None and attempt.ended_at is not None
        detail = json.loads(attempt.error_detail)
        assert detail["provider_identifiers"]["provider_request_id"] == request_id
        assert detail["provider_identifiers"]["provider_response_id"] == "message-local"
        assert await reader.scalar(select(func.count()).select_from(ToolInvocation).where(
            ToolInvocation.run_id == run_id,
        )) == 0
