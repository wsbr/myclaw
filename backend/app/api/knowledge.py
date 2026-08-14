"""Owned local KnowledgeBase administration and retrieval APIs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.deps import CurrentAccount, get_current_account
from app.models.common import utcnow
from app.models.knowledge_v1 import (
    KnowledgeBase,
    KnowledgeDocument,
    KnowledgeSource,
)
from app.persistence.reliable_writes import PlatformAuditWriter
from app.services.knowledge_service import (
    KnowledgeError,
    build_index_revision,
    create_knowledge_base,
    delete_document,
    document_view,
    index_revision_view,
    ingest_source,
    knowledge_base_view,
    owned_knowledge_base,
    replace_agent_access,
    search_knowledge,
    source_view,
)


router = APIRouter(prefix="/knowledge-bases", tags=["knowledge"])


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    agent_ids: list[str] = Field(default_factory=list, max_length=100)


class KnowledgeAccessPatch(BaseModel):
    agent_ids: list[str] = Field(default_factory=list, max_length=100)


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=8, ge=1, le=20)


def _raise(error: KnowledgeError):
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": str(error)},
    ) from error


@router.get("")
async def list_knowledge_bases(
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    items = list(await db.scalars(select(KnowledgeBase).where(
        KnowledgeBase.tenant_id == current.tenant_id,
        KnowledgeBase.owner_account_id == current.account.id,
        KnowledgeBase.status == "active",
    ).order_by(KnowledgeBase.updated_at.desc(), KnowledgeBase.id.desc())))
    return {"items": [await knowledge_base_view(db, item) for item in items]}


@router.post("", status_code=201)
async def add_knowledge_base(
    body: KnowledgeBaseCreate,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    try:
        item = await create_knowledge_base(
            db, tenant_id=current.tenant_id, account_id=current.account.id,
            name=body.name, description=body.description,
            agent_ids=body.agent_ids,
        )
    except KnowledgeError as error:
        _raise(error)
    await PlatformAuditWriter(db).append(
        category="knowledge", action="knowledge_base.created",
        actor_type="account", actor_id=current.account.id,
        actor_user_id=current.account.user_id,
        resource_type="knowledge_base", resource_id=item.id,
        resource_version=item.version, outcome="succeeded",
        summary="Local knowledge base created",
        changes={"sharing_policy": item.sharing_policy},
        dedupe_key=f"audit:knowledge_base:create:{item.id}",
    )
    await db.commit()
    return await knowledge_base_view(db, item)


@router.get("/{knowledge_base_id}")
async def get_knowledge_base(
    knowledge_base_id: str,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    try:
        item = await owned_knowledge_base(
            db, tenant_id=current.tenant_id, account_id=current.account.id,
            knowledge_base_id=knowledge_base_id,
        )
    except KnowledgeError as error:
        _raise(error)
    return await knowledge_base_view(db, item)


@router.patch("/{knowledge_base_id}/access")
async def patch_knowledge_access(
    knowledge_base_id: str,
    body: KnowledgeAccessPatch,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    try:
        item = await owned_knowledge_base(
            db, tenant_id=current.tenant_id, account_id=current.account.id,
            knowledge_base_id=knowledge_base_id, lock=True,
        )
        agent_ids = await replace_agent_access(
            db, knowledge_base=item, account_id=current.account.id,
            agent_ids=body.agent_ids,
        )
    except KnowledgeError as error:
        _raise(error)
    await PlatformAuditWriter(db).append(
        category="knowledge", action="knowledge_base.access_replaced",
        actor_type="account", actor_id=current.account.id,
        actor_user_id=current.account.user_id,
        resource_type="knowledge_base", resource_id=item.id,
        resource_version=item.version, outcome="succeeded",
        summary="Knowledge Agent access replaced",
        changes={"agent_ids": agent_ids},
        dedupe_key=f"audit:knowledge_base:access:{item.id}:v{item.version}",
    )
    await db.commit()
    return await knowledge_base_view(db, item)


@router.get("/{knowledge_base_id}/documents")
async def list_knowledge_documents(
    knowledge_base_id: str,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    try:
        item = await owned_knowledge_base(
            db, tenant_id=current.tenant_id, account_id=current.account.id,
            knowledge_base_id=knowledge_base_id,
        )
    except KnowledgeError as error:
        _raise(error)
    documents = list(await db.scalars(select(KnowledgeDocument).where(
        KnowledgeDocument.tenant_id == current.tenant_id,
        KnowledgeDocument.knowledge_base_id == item.id,
        KnowledgeDocument.status == "active",
    ).order_by(KnowledgeDocument.updated_at.desc(), KnowledgeDocument.id.desc())))
    source_ids = [document.current_source_id for document in documents if document.current_source_id]
    sources = {
        source.id: source for source in await db.scalars(select(KnowledgeSource).where(
            KnowledgeSource.tenant_id == current.tenant_id,
            KnowledgeSource.id.in_(source_ids),
        ))
    } if source_ids else {}
    return {"items": [
        document_view(document, sources.get(document.current_source_id))
        for document in documents
    ]}


@router.post("/{knowledge_base_id}/documents", status_code=201)
async def upload_knowledge_document(
    knowledge_base_id: str,
    source_file: UploadFile = File(alias="file"),
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    try:
        item = await owned_knowledge_base(
            db, tenant_id=current.tenant_id, account_id=current.account.id,
            knowledge_base_id=knowledge_base_id, lock=True,
        )
        data = await source_file.read(settings.knowledge_max_source_bytes + 1)
        document, source, revision, replay = await ingest_source(
            db, knowledge_base=item, account_id=current.account.id,
            filename=source_file.filename or "", data=data,
        )
    except KnowledgeError as error:
        _raise(error)
    await PlatformAuditWriter(db).append(
        category="knowledge", action="knowledge_source.ingested",
        actor_type="account", actor_id=current.account.id,
        actor_user_id=current.account.user_id,
        resource_type="knowledge_source", resource_id=source.id,
        outcome="succeeded" if source.status == "indexed" else "failed",
        summary="Local knowledge source ingested",
        changes={
            "knowledge_base_id": item.id,
            "checksum": source.checksum,
            "status": source.status,
            "replay": replay,
        },
        dedupe_key=f"audit:knowledge_source:ingest:{source.id}",
    )
    await db.commit()
    return {
        "document": document_view(document, source),
        "source": source_view(source),
        "index_revision": index_revision_view(revision) if revision else None,
        "idempotent_replay": replay,
    }


@router.post("/{knowledge_base_id}/reindex")
async def reindex_knowledge_base(
    knowledge_base_id: str,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    try:
        item = await owned_knowledge_base(
            db, tenant_id=current.tenant_id, account_id=current.account.id,
            knowledge_base_id=knowledge_base_id, lock=True,
        )
        revision = await build_index_revision(
            db, knowledge_base=item, account_id=current.account.id,
        )
    except KnowledgeError as error:
        _raise(error)
    await db.commit()
    return index_revision_view(revision)


@router.post("/{knowledge_base_id}/search")
async def search_one_knowledge_base(
    knowledge_base_id: str,
    body: KnowledgeSearchRequest,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    try:
        return await search_knowledge(
            db, tenant_id=current.tenant_id, account_id=current.account.id,
            query=body.query, agent_id=None,
            knowledge_base_id=knowledge_base_id, limit=body.limit,
        )
    except KnowledgeError as error:
        _raise(error)


@router.delete("/{knowledge_base_id}/documents/{document_id}")
async def remove_knowledge_document(
    knowledge_base_id: str, document_id: str,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    try:
        item = await owned_knowledge_base(
            db, tenant_id=current.tenant_id, account_id=current.account.id,
            knowledge_base_id=knowledge_base_id, lock=True,
        )
        document, revision = await delete_document(
            db, knowledge_base=item, document_id=document_id,
            account_id=current.account.id,
        )
    except KnowledgeError as error:
        _raise(error)
    await PlatformAuditWriter(db).append(
        category="knowledge", action="knowledge_document.deleted",
        actor_type="account", actor_id=current.account.id,
        actor_user_id=current.account.user_id,
        resource_type="knowledge_document", resource_id=document.id,
        resource_version=document.version, outcome="succeeded",
        summary="Knowledge document tombstoned and removed from retrieval",
        changes={"index_revision_id": revision.id},
        dedupe_key=f"audit:knowledge_document:delete:{document.id}:v{document.version}",
    )
    await db.commit()
    return {
        "document": document_view(document, None),
        "index_revision": index_revision_view(revision),
    }


@router.delete("/{knowledge_base_id}")
async def remove_knowledge_base(
    knowledge_base_id: str,
    db: AsyncSession = Depends(get_db),
    current: CurrentAccount = Depends(get_current_account),
):
    try:
        item = await owned_knowledge_base(
            db, tenant_id=current.tenant_id, account_id=current.account.id,
            knowledge_base_id=knowledge_base_id, lock=True,
        )
    except KnowledgeError as error:
        _raise(error)
    item.status = "deleted"
    item.version += 1
    documents = list(await db.scalars(select(KnowledgeDocument).where(
        KnowledgeDocument.tenant_id == current.tenant_id,
        KnowledgeDocument.knowledge_base_id == item.id,
        KnowledgeDocument.status == "active",
    ).with_for_update()))
    for document in documents:
        document.status = "deleted"
        document.deleted_at = utcnow()
        document.version += 1
        if document.current_source_id:
            source = await db.get(KnowledgeSource, document.current_source_id)
            if source is not None:
                source.status = "deleted"
    await PlatformAuditWriter(db).append(
        category="knowledge", action="knowledge_base.deleted",
        actor_type="account", actor_id=current.account.id,
        actor_user_id=current.account.user_id,
        resource_type="knowledge_base", resource_id=item.id,
        resource_version=item.version, outcome="succeeded",
        summary="Knowledge base tombstoned",
        dedupe_key=f"audit:knowledge_base:delete:{item.id}:v{item.version}",
    )
    await db.commit()
    return {"id": item.id, "status": item.status, "version": item.version}
