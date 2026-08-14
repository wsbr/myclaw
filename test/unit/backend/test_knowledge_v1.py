"""Local KnowledgeBase indexing, citations, snapshots, and isolation."""

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models.agent_v1 import AgentBlueprint
from app.models.foundation import PlatformAccount, UserIdentity, UserSession
from app.models.knowledge_v1 import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeIndexRevision,
    KnowledgeLexiconRevision,
    KnowledgeRetrievalSnapshot,
    KnowledgeSource,
)
from app.models.runtime_v1 import DurableRunner, Run, WorkItem
from app.models.tenant_v1 import Tenant
from app.services.knowledge_service import (
    KnowledgeError,
    create_knowledge_base,
    delete_document,
    ingest_source,
    parse_source_bytes,
    reconcile_knowledge_storage,
    replace_knowledge_lexicon,
    search_knowledge,
)


pytestmark = pytest.mark.unit

TENANT_A = "01d00000-0000-7000-8000-000000000001"
TENANT_B = "01d00000-0000-7000-8000-000000000002"
ACCOUNT_A = "01d00000-0000-7000-8000-000000000011"
ACCOUNT_B = "01d00000-0000-7000-8000-000000000012"
AGENT_A = "01d00000-0000-7000-8000-000000000021"
AGENT_B = "01d00000-0000-7000-8000-000000000022"
SESSION_A = "01d00000-0000-7000-8000-000000000031"
RUN_A = "01d00000-0000-7000-8000-000000000041"
WORK_A = "01d00000-0000-7000-8000-000000000051"
RUNNER_A = "01d00000-0000-7000-8000-000000000061"


@pytest.fixture
async def knowledge_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "knowledge_root", str(tmp_path / "knowledge"))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as db:
        db.add_all([
            Tenant(id=TENANT_A, slug="alpha", name="Alpha", status="active"),
            Tenant(id=TENANT_B, slug="beta", name="Beta", status="active"),
            UserIdentity(user_id="alice", status="active"),
            UserIdentity(user_id="bob", status="active"),
            PlatformAccount(
                id=ACCOUNT_A, tenant_id=TENANT_A, user_id="alice",
                display_name="Alice", status="active",
            ),
            PlatformAccount(
                id=ACCOUNT_B, tenant_id=TENANT_B, user_id="bob",
                display_name="Bob", status="active",
            ),
            AgentBlueprint(
                id=AGENT_A, tenant_id=TENANT_A, owner_account_id=ACCOUNT_A,
                visibility="private", status="draft",
            ),
            AgentBlueprint(
                id=AGENT_B, tenant_id=TENANT_B, owner_account_id=ACCOUNT_B,
                visibility="private", status="draft",
            ),
            UserSession(
                id=SESSION_A, tenant_id=TENANT_A, account_id=ACCOUNT_A,
                user_id="alice", entry_agent_id=AGENT_A, status="active",
            ),
            Run(
                id=RUN_A, tenant_id=TENANT_A, account_id=ACCOUNT_A,
                session_id=SESSION_A, user_id="alice", mode="chat",
                status="active", permission_mode="direct",
            ),
            WorkItem(
                id=WORK_A, tenant_id=TENANT_A, run_id=RUN_A,
                session_id=SESSION_A, user_id="alice", mode="chat",
                owner_agent_id=AGENT_A, agent_version_id="agent-version-a",
                goal="Use local evidence", status="running",
            ),
            DurableRunner(
                id=RUNNER_A, tenant_id=TENANT_A, work_item_id=WORK_A,
                run_id=RUN_A, session_id=SESSION_A, agent_id=AGENT_A,
                agent_version_id="agent-version-a", execution_epoch=1,
                status="running",
            ),
        ])
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_ingest_is_checksum_idempotent_and_changed_content_revisions(knowledge_db):
    async with knowledge_db.begin() as db:
        base = await create_knowledge_base(
            db, tenant_id=TENANT_A, account_id=ACCOUNT_A,
            name="Operations", description="Local operational evidence",
            agent_ids=[AGENT_A],
        )
        first = b"# Recovery\n\nUse immutable checkpoints before resuming a runner."
        document, source_1, revision_1, replay = await ingest_source(
            db, knowledge_base=base, account_id=ACCOUNT_A,
            filename="guide.md", data=first,
        )
        assert replay is False
        assert source_1.status == "indexed"
        assert revision_1.revision_no == 1
        assert revision_1.chunk_count >= 1

        same_document, same_source, same_revision, replay = await ingest_source(
            db, knowledge_base=base, account_id=ACCOUNT_A,
            filename="duplicate-name.md", data=first,
        )
        assert replay is True
        assert same_document.id == document.id
        assert same_source.id == source_1.id
        assert same_revision.id == revision_1.id

        changed = (
            b"# Recovery\n\nUse immutable checkpoints before resuming a runner.\n\n"
            b"Verify the fencing token before every settlement."
        )
        document_2, source_2, revision_2, replay = await ingest_source(
            db, knowledge_base=base, account_id=ACCOUNT_A,
            filename="guide.md", data=changed,
        )
        assert replay is False
        assert document_2.id == document.id
        assert source_2.id != source_1.id
        assert revision_2.revision_no == 2
        assert source_1.status == "superseded"
        base_id = base.id
        document_id = document.id
        source_2_checksum = source_2.checksum

    async with knowledge_db.begin() as db:
        result = await search_knowledge(
            db, tenant_id=TENANT_A, account_id=ACCOUNT_A,
            agent_id=AGENT_A, runner_id=RUNNER_A,
            knowledge_base_id=base_id, query="fencing token settlement", limit=5,
        )
        assert result["count"] >= 1
        citation = result["results"][0]["citation"]
        assert citation["checksum"] == source_2_checksum
        assert citation["index_revision_id"] == result["revision_manifest"][0]["index_revision_id"]
        assert await db.scalar(select(func.count()).select_from(
            KnowledgeRetrievalSnapshot
        )) == 1

        base = await db.get(KnowledgeBase, base_id)
        _, deletion_revision = await delete_document(
            db, knowledge_base=base, document_id=document_id,
            account_id=ACCOUNT_A,
        )
        assert deletion_revision.revision_no == 3
        assert deletion_revision.chunk_count == 0

    async with knowledge_db() as db:
        after_delete = await search_knowledge(
            db, tenant_id=TENANT_A, account_id=ACCOUNT_A,
            agent_id=None, knowledge_base_id=base_id,
            query="fencing token", limit=5,
        )
        assert after_delete["results"] == []
        assert await db.scalar(select(func.count()).select_from(
            KnowledgeIndexRevision
        )) == 3
        assert await db.scalar(select(func.count()).select_from(KnowledgeChunk)) >= 2


@pytest.mark.asyncio
async def test_agent_account_and_tenant_access_fail_closed(knowledge_db):
    async with knowledge_db.begin() as db:
        base = await create_knowledge_base(
            db, tenant_id=TENANT_A, account_id=ACCOUNT_A,
            name="Private", description="Private", agent_ids=[AGENT_A],
        )
        await ingest_source(
            db, knowledge_base=base, account_id=ACCOUNT_A,
            filename="secret.txt", data=b"Project codename is Northstar.",
        )
        base_id = base.id

    async with knowledge_db() as db:
        with pytest.raises(KnowledgeError) as cross_tenant:
            await search_knowledge(
                db, tenant_id=TENANT_B, account_id=ACCOUNT_B,
                agent_id=AGENT_B, knowledge_base_id=base_id,
                query="codename", limit=5,
            )
        assert cross_tenant.value.status_code == 404

        with pytest.raises(KnowledgeError) as ungranted:
            await search_knowledge(
                db, tenant_id=TENANT_A, account_id=ACCOUNT_A,
                agent_id=AGENT_B, knowledge_base_id=base_id,
                query="codename", limit=5,
            )
        assert ungranted.value.status_code == 404

        with pytest.raises(KnowledgeError) as other_account:
            await search_knowledge(
                db, tenant_id=TENANT_A, account_id=ACCOUNT_B,
                agent_id=None, knowledge_base_id=base_id,
                query="codename", limit=5,
            )
        assert other_account.value.status_code == 404


def test_bounded_parser_rejects_non_utf8_and_invalid_pdf():
    with pytest.raises(KnowledgeError) as text_error:
        parse_source_bytes(b"\xff\xfe", media_type="text/plain")
    assert text_error.value.code == "knowledge_text_not_utf8"

    with pytest.raises(KnowledgeError) as pdf_error:
        parse_source_bytes(b"not-a-pdf", media_type="application/pdf")
    assert pdf_error.value.code == "knowledge_pdf_invalid"


@pytest.mark.asyncio
async def test_domain_lexicon_revisions_are_immutable_idempotent_and_unambiguous(knowledge_db):
    async with knowledge_db.begin() as db:
        base = await create_knowledge_base(
            db, tenant_id=TENANT_A, account_id=ACCOUNT_A,
            name="Lexicon", description="Domain aliases", agent_ids=[],
        )
        first, index_revision, replay = await replace_knowledge_lexicon(
            db, knowledge_base=base, account_id=ACCOUNT_A,
            raw_terms=[{
                "canonical_name": "checkpoint recovery",
                "term_type": "concept", "description": "durable resume",
                "aliases": ["断点续跑"],
            }],
        )
        assert index_revision is None
        assert replay is False
        assert base.current_lexicon_revision_id == first.id
        assert first.revision_no == 1
        replayed, _, replay = await replace_knowledge_lexicon(
            db, knowledge_base=base, account_id=ACCOUNT_A,
            raw_terms=[{
                "canonical_name": "checkpoint recovery",
                "term_type": "concept", "description": "durable resume",
                "aliases": ["断点续跑"],
            }],
        )
        assert replay is True
        assert replayed.id == first.id
        second, _, replay = await replace_knowledge_lexicon(
            db, knowledge_base=base, account_id=ACCOUNT_A,
            raw_terms=[{
                "canonical_name": "checkpoint recovery",
                "term_type": "concept", "description": "durable resume",
                "aliases": ["断点续跑", "检查点恢复"],
            }],
        )
        assert replay is False
        assert second.revision_no == 2
        assert first.terms[0]["aliases"] != second.terms[0]["aliases"]
        with pytest.raises(KnowledgeError) as ambiguous:
            await replace_knowledge_lexicon(
                db, knowledge_base=base, account_id=ACCOUNT_A,
                raw_terms=[
                    {"canonical_name": "alpha", "aliases": ["shared"]},
                    {"canonical_name": "beta", "aliases": ["shared"]},
                ],
            )
        assert ambiguous.value.code == "knowledge_lexicon_invalid"
        assert await db.scalar(select(func.count()).select_from(
            KnowledgeLexiconRevision
        )) == 2


@pytest.mark.asyncio
async def test_storage_reconciliation_fails_closed_and_restores_exact_body(knowledge_db):
    body = b"Recovery requires the exact durable fencing token."
    async with knowledge_db.begin() as db:
        base = await create_knowledge_base(
            db, tenant_id=TENANT_A, account_id=ACCOUNT_A,
            name="Reconciliation", description="Storage recovery",
            agent_ids=[AGENT_A],
        )
        _, source, first_revision, _ = await ingest_source(
            db, knowledge_base=base, account_id=ACCOUNT_A,
            filename="recovery.txt", data=body,
        )
        base_id = base.id
        source_id = source.id
        storage_key = source.storage_key
        assert first_revision.revision_no == 1
        first_revision_id = first_revision.id

    source_path = Path(settings.knowledge_root) / storage_key
    source_path.unlink()
    orphan_path = source_path.parent / "orphaned-body.txt"
    orphan_path.write_bytes(b"not referenced by authoritative metadata")

    async with knowledge_db.begin() as db:
        unavailable = await search_knowledge(
            db, tenant_id=TENANT_A, account_id=ACCOUNT_A,
            agent_id=AGENT_A, knowledge_base_id=base_id,
            query="fencing token", limit=5,
        )
        assert unavailable["results"] == []
        base = await db.get(KnowledgeBase, base_id)
        report = await reconcile_knowledge_storage(
            db, knowledge_base=base, account_id=ACCOUNT_A, repair=True,
        )
        assert [item["source_id"] for item in report["missing_sources"]] == [source_id]
        assert report["orphaned_bodies"] == [
            orphan_path.relative_to(Path(settings.knowledge_root)).as_posix(),
        ]
        assert report["changed_source_ids"] == [source_id]
        assert report["index_revision"]["revision_no"] == 2
        assert report["index_revision"]["chunk_count"] == 0
        source = await db.get(KnowledgeSource, source_id)
        assert source.status == "failed"
        assert source.error_code == "knowledge_source_body_missing"

    source_path.write_bytes(body)
    async with knowledge_db.begin() as db:
        base = await db.get(KnowledgeBase, base_id)
        restored = await reconcile_knowledge_storage(
            db, knowledge_base=base, account_id=ACCOUNT_A, repair=True,
        )
        assert restored["restored_source_ids"] == [source_id]
        assert restored["index_revision"]["id"] == first_revision_id
        assert restored["index_revision"]["revision_no"] == 1
        assert restored["index_revision"]["chunk_count"] >= 1
        source = await db.get(KnowledgeSource, source_id)
        assert source.status == "indexed"
        assert source.error_code is None

    async with knowledge_db() as db:
        available = await search_knowledge(
            db, tenant_id=TENANT_A, account_id=ACCOUNT_A,
            agent_id=AGENT_A, knowledge_base_id=base_id,
            query="fencing token", limit=5,
        )
        assert available["results"]
