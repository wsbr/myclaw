"""Real MySQL visibility and revocation checks with immutable relations enabled.

Uses the disposable *_test database and complete graph fixture shared by the
collaboration integration suite. No model or external Tool is executed.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select, text

from app.models.artifact_v1 import Artifact, MessageArtifact
from app.models.common import uuid7
from app.models.memory_v1 import MemoryHead, MemoryRecord, RunnerMemorySnapshot
from app.models.runtime_v1 import Message, SessionSandbox, Workspace
from app.services.context_service import assemble_standard_context
from app.services.memory_service import normalized_content, recall_memories
from integration.test_agent_collaboration_mysql import (
    invoke, invoked_probe, mysql_collaboration, publish, root_probe,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_mysql_context_metadata_and_same_runner_revocation(mysql_collaboration):
    f = mysql_collaboration
    async with f.factory() as db:
        child_agent = await publish(db, f, "Context child")
        parent_agent = await publish(db, f, "Context parent", bound=(child_agent.agent.id,))
        await db.commit()
        parent = await root_probe(db, f, parent_agent)
        child, _ = await invoked_probe(db, await invoke(
            db, parent, child_agent.agent.id, call_id="context-child",
        ))
        sandbox = SessionSandbox(
            id=uuid7(), session_id=parent.run.session_id, sandbox_profile_id=f.sandbox_id,
            profile_config_version=1, status="ready", root_uri="file:///context-boundary-test",
        )
        db.add(sandbox)
        await db.flush()
        workspace = Workspace(
            id=uuid7(), tenant_id=f.tenant_id, session_sandbox_id=sandbox.id,
            session_id=parent.run.session_id, root_uri="file:///context-boundary-test", status="active",
        )
        db.add(workspace)
        await db.flush()

        def artifact(probe):
            identity = uuid7()
            row = Artifact(
                id=identity, tenant_id=f.tenant_id, user_id=f.user_id,
                session_id=parent.run.session_id, workspace_id=workspace.id,
                relative_path=f"private/{identity}.txt", name=f"{identity}.txt", mime_type="text/plain",
                size_bytes=1, checksum="a" * 64, publication_key=identity.replace("-", "").ljust(64, "0"),
                created_by_agent_id=probe.runner.agent_id, work_item_id=probe.work.id,
                runner_id=probe.runner.id, status="published", version_no=1,
            )
            db.add(row)
            return row

        parent_artifact, child_private, child_delivered = artifact(parent), artifact(child), artifact(child)
        await db.flush()
        message = Message(
            id=uuid7(), session_id=parent.run.session_id, user_id=f.user_id, scope_type="chat",
            sender_type="agent", sender_agent_id=child.runner.agent_id, recipient_type="session",
            role="assistant", content="Explicit child delivery", visibility="user", status="committed",
        )
        db.add(message)
        await db.flush()
        db.add(MessageArtifact(message_id=message.id, artifact_id=child_delivered.id, visibility="user"))

        memories = []
        for label in ("revoked", "retained"):
            head = MemoryHead(
                id=uuid7(), tenant_id=f.tenant_id, user_id=f.user_id,
                agent_id=parent.runner.agent_id, scope="user_agent", fact_key=label,
            )
            db.add(head)
            await db.flush()
            content, digest = normalized_content(f"Context boundary {label}")
            memory = MemoryRecord(
                id=uuid7(), tenant_id=f.tenant_id, head_id=head.id, user_id=f.user_id,
                agent_id=parent.runner.agent_id, scope="user_agent", fact_key=label,
                content=content, normalized_hash=digest, status="active", source_kind="import",
            )
            db.add(memory)
            memories.append(memory)
        await db.commit()

        async def selected(probe):
            result = await assemble_standard_context(
                db, run=probe.run, runner=probe.runner, policy=None, parameters={"max_artifacts": 20},
            )
            ids = {item["artifact_id"] for item in json.loads(result.content)["published_artifacts"]}
            assert ids == set(result.manifest["source_ids"]["artifact_ids"])
            return ids

        assert await selected(parent) == {parent_artifact.id, child_delivered.id}
        assert await selected(child) == {child_private.id, child_delivered.id}
        recall_args = dict(
            tenant_id=f.tenant_id, runner_id=parent.runner.id, user_id=f.user_id,
            agent_id=parent.runner.agent_id, policy_key="memory_recall.user_combined",
        )
        assert {row.id for row in await recall_memories(db, **recall_args)} == {row.id for row in memories}
        await db.commit()
        snapshots_before = list((await db.execute(select(
            RunnerMemorySnapshot.memory_id, RunnerMemorySnapshot.content_hash, RunnerMemorySnapshot.rank,
        ).where(RunnerMemorySnapshot.runner_id == parent.runner.id))).all())
        # A second transaction models user deletion while the original Session
        # still holds its already-loaded MemoryRecord and recall relation.
        async with f.factory.begin() as writer:
            revoked = await writer.get(MemoryRecord, memories[0].id)
            revoked.status = "deleted"
            revoked.version += 1
        assert [row.id for row in await recall_memories(db, **recall_args)] == [memories[1].id]
        assert await recall_memories(db, **{**recall_args, "policy_key": "memory_recall.none"}) == []
        snapshots_after = list((await db.execute(select(
            RunnerMemorySnapshot.memory_id, RunnerMemorySnapshot.content_hash, RunnerMemorySnapshot.rank,
        ).where(RunnerMemorySnapshot.runner_id == parent.runner.id))).all())
        assert snapshots_after == snapshots_before
        assert await db.scalar(text("SELECT @@foreign_key_checks")) == 1
        assert await db.scalar(text(
            "SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() "
            "AND EVENT_OBJECT_TABLE='runner_memory_snapshot'"
        )) > 0
