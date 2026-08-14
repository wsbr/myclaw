"""Local document ingestion, immutable indexing, retrieval, and citations."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

from pypdf import PdfReader
from sqlalchemy import delete, or_, select

from app.config import settings
from app.models.agent_v1 import AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.knowledge_v1 import (
    KnowledgeBase,
    KnowledgeBaseAgentAccess,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIndexRevision,
    KnowledgeRetrievalSnapshot,
    KnowledgeSource,
)
from app.models.runtime_v1 import DurableRunner


DEFAULT_CHUNKING_PROFILE = {
    "profile": "local.characters.v1",
    "chunk_chars": 1200,
    "overlap_chars": 150,
}
DEFAULT_EMBEDDING_PROFILE = {
    "profile": "local.feature_hash.v1",
    "dimensions": 256,
    "external_service": False,
    "lexical_fallback": "local.term_frequency.v1",
}
SAFE_FILENAME = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,500}$")
TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]", re.I)
SUPPORTED_EXTENSIONS = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".pdf": "application/pdf",
}


class KnowledgeError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 422):
        self.code = code
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class ParsedBlock:
    content: str
    page_number: int | None = None
    section: str | None = None


@dataclass(frozen=True)
class ChunkDraft:
    content: str
    page_number: int | None
    section: str | None
    terms: dict[str, int]
    vector: dict[str, float]
    token_count: int


def _canonical_json(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _digest(value) -> str:
    raw = value if isinstance(value, bytes) else _canonical_json(value)
    return hashlib.sha256(raw).hexdigest()


def normalize_filename(filename: str) -> tuple[str, str, str]:
    display = unicodedata.normalize("NFKC", str(filename or "")).strip()
    if not SAFE_FILENAME.fullmatch(display) or display in {".", ".."}:
        raise KnowledgeError("knowledge_filename_invalid", "A safe filename is required")
    extension = Path(display).suffix.casefold()
    media_type = SUPPORTED_EXTENSIONS.get(extension)
    if media_type is None:
        raise KnowledgeError(
            "knowledge_media_type_unsupported",
            "Only UTF-8 text, Markdown, and PDF sources are supported",
        )
    return display, display.casefold(), media_type


def parse_source_bytes(data: bytes, *, media_type: str) -> list[ParsedBlock]:
    if not data:
        raise KnowledgeError("knowledge_source_empty", "The source file is empty")
    if media_type in {"text/plain", "text/markdown"}:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise KnowledgeError(
                "knowledge_text_not_utf8", "Text and Markdown must be valid UTF-8",
            ) from error
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            raise KnowledgeError("knowledge_source_empty", "The source has no text")
        if media_type == "text/plain":
            return [ParsedBlock(text)]
        blocks: list[ParsedBlock] = []
        current_section = None
        buffer: list[str] = []
        for line in text.splitlines():
            heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
            if heading:
                if "\n".join(buffer).strip():
                    blocks.append(ParsedBlock(
                        "\n".join(buffer).strip(), section=current_section,
                    ))
                current_section = heading.group(1).strip()[:500]
                buffer = [line]
            else:
                buffer.append(line)
        if "\n".join(buffer).strip():
            blocks.append(ParsedBlock(
                "\n".join(buffer).strip(), section=current_section,
            ))
        return blocks
    if media_type == "application/pdf":
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
        except Exception as error:
            raise KnowledgeError(
                "knowledge_pdf_invalid", "The PDF could not be parsed",
            ) from error
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:
                unlocked = 0
            if not unlocked:
                raise KnowledgeError(
                    "knowledge_pdf_encrypted", "Encrypted PDFs are not supported",
                )
        if len(reader.pages) > settings.knowledge_max_pdf_pages:
            raise KnowledgeError(
                "knowledge_pdf_page_limit",
                f"PDF exceeds the {settings.knowledge_max_pdf_pages}-page limit",
            )
        blocks = []
        for page_number, page in enumerate(reader.pages, 1):
            try:
                text = (page.extract_text() or "").strip()
            except Exception as error:
                raise KnowledgeError(
                    "knowledge_pdf_extract_failed",
                    f"Text extraction failed on PDF page {page_number}",
                ) from error
            if text:
                blocks.append(ParsedBlock(text, page_number=page_number))
        if not blocks:
            raise KnowledgeError(
                "knowledge_pdf_no_text",
                "The PDF contains no extractable text; OCR is not included in this release",
            )
        return blocks
    raise KnowledgeError("knowledge_media_type_unsupported", "Unsupported source type")


def tokenize(text: str) -> list[str]:
    base = [match.group(0).casefold() for match in TOKEN_PATTERN.finditer(text)]
    values = list(base)
    cjk_run: list[str] = []
    for item in base + [" "]:
        if len(item) == 1 and "\u3400" <= item <= "\u9fff":
            cjk_run.append(item)
            continue
        if len(cjk_run) > 1:
            values.extend(
                cjk_run[index] + cjk_run[index + 1]
                for index in range(len(cjk_run) - 1)
            )
        cjk_run = []
    return values


def feature_vector(terms: dict[str, int], dimensions: int = 256) -> dict[str, float]:
    raw: dict[int, float] = {}
    for term, frequency in terms.items():
        digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = -1.0 if digest[4] & 1 else 1.0
        raw[index] = raw.get(index, 0.0) + sign * (1.0 + math.log(frequency))
    norm = math.sqrt(sum(value * value for value in raw.values())) or 1.0
    return {str(index): round(value / norm, 8) for index, value in raw.items()}


def chunk_blocks(
    blocks: list[ParsedBlock], *, chunk_chars: int, overlap_chars: int,
) -> list[ChunkDraft]:
    if overlap_chars >= chunk_chars:
        raise KnowledgeError(
            "knowledge_chunk_profile_invalid", "Chunk overlap must be smaller than chunk size",
        )
    chunks: list[ChunkDraft] = []
    for block in blocks:
        content = block.content.strip()
        offset = 0
        while offset < len(content):
            boundary = min(len(content), offset + chunk_chars)
            if boundary < len(content):
                candidate = max(
                    content.rfind("\n", offset + chunk_chars // 2, boundary),
                    content.rfind(" ", offset + chunk_chars // 2, boundary),
                )
                if candidate > offset:
                    boundary = candidate
            piece = content[offset:boundary].strip()
            if piece:
                terms = dict(Counter(tokenize(piece)))
                chunks.append(ChunkDraft(
                    content=piece,
                    page_number=block.page_number,
                    section=block.section,
                    terms=terms,
                    vector=feature_vector(terms),
                    token_count=sum(terms.values()),
                ))
            if boundary >= len(content):
                break
            offset = max(offset + 1, boundary - overlap_chars)
    return chunks


def _source_path(storage_key: str) -> Path:
    root = Path(settings.knowledge_root).resolve()
    target = (root / storage_key).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise KnowledgeError(
            "knowledge_storage_key_invalid", "Knowledge source path escaped its root",
            status_code=500,
        ) from error
    return target


def store_source_bytes(
    *, tenant_id: str, knowledge_base_id: str, checksum: str,
    filename: str, data: bytes,
) -> str:
    extension = Path(filename).suffix.casefold()
    storage_key = f"{tenant_id}/{knowledge_base_id}/{checksum[:2]}/{checksum}{extension}"
    target = _source_path(storage_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if _digest(target.read_bytes()) != checksum:
            raise KnowledgeError(
                "knowledge_storage_checksum_conflict",
                "Existing local source body has an invalid checksum",
                status_code=500,
            )
        return storage_key
    temporary = target.with_name(f".{target.name}.{uuid7()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return storage_key


def load_source_bytes(source: KnowledgeSource) -> bytes:
    target = _source_path(source.storage_key)
    if not target.is_file():
        raise KnowledgeError(
            "knowledge_source_body_missing", "Indexed source body is unavailable",
            status_code=500,
        )
    data = target.read_bytes()
    if _digest(data) != source.checksum:
        raise KnowledgeError(
            "knowledge_source_checksum_mismatch", "Indexed source checksum is invalid",
            status_code=500,
        )
    return data


async def owned_knowledge_base(
    db, *, tenant_id: str, account_id: str, knowledge_base_id: str,
    lock: bool = False, include_deleted: bool = False,
) -> KnowledgeBase:
    query = select(KnowledgeBase).where(
        KnowledgeBase.id == knowledge_base_id,
        KnowledgeBase.tenant_id == tenant_id,
        KnowledgeBase.owner_account_id == account_id,
    )
    if not include_deleted:
        query = query.where(KnowledgeBase.status == "active")
    if lock:
        query = query.with_for_update()
    item = await db.scalar(query)
    if item is None:
        raise KnowledgeError("knowledge_base_not_found", "Knowledge base not found", status_code=404)
    return item


async def create_knowledge_base(
    db, *, tenant_id: str, account_id: str, name: str, description: str,
    agent_ids: list[str],
) -> KnowledgeBase:
    name = unicodedata.normalize("NFKC", name).strip()
    if not name or len(name) > 200:
        raise KnowledgeError("knowledge_base_name_invalid", "Knowledge base name is invalid")
    existing = await db.scalar(select(KnowledgeBase).where(
        KnowledgeBase.tenant_id == tenant_id,
        KnowledgeBase.owner_account_id == account_id,
        KnowledgeBase.name == name,
    ))
    if existing is not None:
        if existing.status == "active":
            return existing
        raise KnowledgeError("knowledge_base_name_conflict", "A deleted base retains this name", status_code=409)
    item = KnowledgeBase(
        id=uuid7(), tenant_id=tenant_id, owner_account_id=account_id,
        name=name, description=description.strip(),
        sharing_policy="selected_agents" if agent_ids else "owner_only",
        status="active", next_revision_no=1,
        chunking_profile={
            **DEFAULT_CHUNKING_PROFILE,
            "chunk_chars": settings.knowledge_chunk_chars,
            "overlap_chars": settings.knowledge_chunk_overlap_chars,
        },
        embedding_profile=dict(DEFAULT_EMBEDDING_PROFILE),
    )
    db.add(item)
    await db.flush()
    await replace_agent_access(
        db, knowledge_base=item, account_id=account_id, agent_ids=agent_ids,
        bump_version=False,
    )
    return item


async def replace_agent_access(
    db, *, knowledge_base: KnowledgeBase, account_id: str, agent_ids: list[str],
    bump_version: bool = True,
) -> list[str]:
    normalized = list(dict.fromkeys(agent_ids))
    if len(normalized) > 100:
        raise KnowledgeError("knowledge_agent_limit", "At most 100 Agents may be granted")
    current = await agent_access_ids(
        db, knowledge_base.id, knowledge_base.tenant_id,
    )
    if current == sorted(normalized):
        return current
    if normalized:
        agents = list(await db.scalars(select(AgentBlueprint).where(
            AgentBlueprint.id.in_(normalized),
            AgentBlueprint.tenant_id == knowledge_base.tenant_id,
            AgentBlueprint.status.in_(("draft", "published")),
        )))
        if {item.id for item in agents} != set(normalized):
            raise KnowledgeError(
                "knowledge_agent_unavailable",
                "Every granted Agent must exist in the current Tenant",
                status_code=404,
            )
    await db.execute(delete(KnowledgeBaseAgentAccess).where(
        KnowledgeBaseAgentAccess.knowledge_base_id == knowledge_base.id,
        KnowledgeBaseAgentAccess.tenant_id == knowledge_base.tenant_id,
    ))
    db.add_all([
        KnowledgeBaseAgentAccess(
            id=uuid7(), tenant_id=knowledge_base.tenant_id,
            knowledge_base_id=knowledge_base.id, agent_id=agent_id,
            granted_by_account_id=account_id,
        )
        for agent_id in normalized
    ])
    knowledge_base.sharing_policy = "selected_agents" if normalized else "owner_only"
    if bump_version:
        knowledge_base.version += 1
    await db.flush()
    return normalized


async def agent_access_ids(db, knowledge_base_id: str, tenant_id: str) -> list[str]:
    return list(await db.scalars(select(KnowledgeBaseAgentAccess.agent_id).where(
        KnowledgeBaseAgentAccess.knowledge_base_id == knowledge_base_id,
        KnowledgeBaseAgentAccess.tenant_id == tenant_id,
    ).order_by(KnowledgeBaseAgentAccess.agent_id)))


async def ingest_source(
    db, *, knowledge_base: KnowledgeBase, account_id: str,
    filename: str, data: bytes,
) -> tuple[KnowledgeDocument, KnowledgeSource, KnowledgeIndexRevision | None, bool]:
    if len(data) > settings.knowledge_max_source_bytes:
        raise KnowledgeError(
            "knowledge_source_too_large",
            f"Source exceeds {settings.knowledge_max_source_bytes} bytes",
            status_code=413,
        )
    display_name, logical_key, media_type = normalize_filename(filename)
    checksum = _digest(data)
    existing = await db.scalar(select(KnowledgeSource).where(
        KnowledgeSource.knowledge_base_id == knowledge_base.id,
        KnowledgeSource.tenant_id == knowledge_base.tenant_id,
        KnowledgeSource.checksum == checksum,
    ))
    if existing is not None:
        document = await db.get(KnowledgeDocument, existing.document_id)
        revision = (
            await db.get(KnowledgeIndexRevision, knowledge_base.current_index_revision_id)
            if knowledge_base.current_index_revision_id else None
        )
        return document, existing, revision, True

    document = await db.scalar(select(KnowledgeDocument).where(
        KnowledgeDocument.knowledge_base_id == knowledge_base.id,
        KnowledgeDocument.tenant_id == knowledge_base.tenant_id,
        KnowledgeDocument.logical_key == logical_key,
    ).with_for_update())
    if document is None:
        document = KnowledgeDocument(
            id=uuid7(), tenant_id=knowledge_base.tenant_id,
            knowledge_base_id=knowledge_base.id, logical_key=logical_key,
            title=display_name, status="active",
        )
        db.add(document)
        await db.flush()
    else:
        document.status = "active"
        document.deleted_at = None
        document.title = display_name
        document.version += 1

    parse_error = None
    blocks: list[ParsedBlock] = []
    try:
        blocks = await asyncio.to_thread(
            parse_source_bytes, data, media_type=media_type,
        )
    except KnowledgeError as error:
        parse_error = error
    storage_key = await asyncio.to_thread(
        store_source_bytes,
        tenant_id=knowledge_base.tenant_id,
        knowledge_base_id=knowledge_base.id,
        checksum=checksum, filename=display_name, data=data,
    )
    previous = (
        await db.get(KnowledgeSource, document.current_source_id)
        if document.current_source_id else None
    )
    source = KnowledgeSource(
        id=uuid7(), tenant_id=knowledge_base.tenant_id,
        knowledge_base_id=knowledge_base.id, document_id=document.id,
        checksum=checksum, filename=display_name, media_type=media_type,
        size_bytes=len(data), storage_key=storage_key,
        status="failed" if parse_error else "indexed",
        parse_metadata={
            "parser": {
                "text/plain": "utf8-text.v1",
                "text/markdown": "markdown-headings.v1",
                "application/pdf": "pypdf.v1",
            }[media_type],
            "block_count": len(blocks),
            "page_count": max(
                (item.page_number or 0 for item in blocks), default=0,
            ) or None,
        },
        error_code=parse_error.code if parse_error else None,
        error_detail=str(parse_error) if parse_error else None,
        created_by_account_id=account_id,
    )
    db.add(source)
    await db.flush()
    if parse_error is not None:
        return document, source, None, False
    if previous is not None and previous.status == "indexed":
        previous.status = "superseded"
    document.current_source_id = source.id
    await db.flush()
    revision = await build_index_revision(
        db, knowledge_base=knowledge_base, account_id=account_id,
    )
    return document, source, revision, False


async def build_index_revision(
    db, *, knowledge_base: KnowledgeBase, account_id: str,
) -> KnowledgeIndexRevision:
    documents = list(await db.scalars(select(KnowledgeDocument).where(
        KnowledgeDocument.tenant_id == knowledge_base.tenant_id,
        KnowledgeDocument.knowledge_base_id == knowledge_base.id,
        KnowledgeDocument.status == "active",
        KnowledgeDocument.current_source_id.is_not(None),
    ).order_by(KnowledgeDocument.id).with_for_update()))
    source_ids = [item.current_source_id for item in documents]
    sources = list(await db.scalars(select(KnowledgeSource).where(
        KnowledgeSource.tenant_id == knowledge_base.tenant_id,
        KnowledgeSource.id.in_(source_ids),
        KnowledgeSource.status == "indexed",
    ))) if source_ids else []
    source_by_id = {item.id: item for item in sources}
    manifest = [{
        "document_id": item.id,
        "source_id": item.current_source_id,
        "checksum": source_by_id[item.current_source_id].checksum,
        "filename": source_by_id[item.current_source_id].filename,
    } for item in documents if item.current_source_id in source_by_id]
    manifest_hash = _digest({
        "sources": manifest,
        "chunking_profile": knowledge_base.chunking_profile,
        "embedding_profile": knowledge_base.embedding_profile,
    })
    current = (
        await db.get(KnowledgeIndexRevision, knowledge_base.current_index_revision_id)
        if knowledge_base.current_index_revision_id else None
    )
    if current is not None and current.source_manifest_hash == manifest_hash:
        return current

    drafts: list[tuple[KnowledgeDocument, KnowledgeSource, ChunkDraft]] = []
    for document in documents:
        source = source_by_id.get(document.current_source_id)
        if source is None:
            continue
        source_data = await asyncio.to_thread(load_source_bytes, source)
        blocks = await asyncio.to_thread(
            parse_source_bytes, source_data, media_type=source.media_type,
        )
        source_chunks = await asyncio.to_thread(
            chunk_blocks,
            blocks,
            chunk_chars=int(knowledge_base.chunking_profile["chunk_chars"]),
            overlap_chars=int(knowledge_base.chunking_profile["overlap_chars"]),
        )
        for chunk in source_chunks:
            drafts.append((document, source, chunk))
            if len(drafts) > settings.knowledge_max_chunks_per_base:
                raise KnowledgeError(
                    "knowledge_chunk_limit",
                    f"Knowledge base exceeds {settings.knowledge_max_chunks_per_base} chunks",
                )
    chunk_hashes = [_digest(item.content.encode("utf-8")) for _, _, item in drafts]
    content_hash = _digest({
        "knowledge_base_id": knowledge_base.id,
        "manifest_hash": manifest_hash,
        "chunk_hashes": chunk_hashes,
    })
    replay = await db.scalar(select(KnowledgeIndexRevision).where(
        KnowledgeIndexRevision.knowledge_base_id == knowledge_base.id,
        KnowledgeIndexRevision.content_hash == content_hash,
    ))
    if replay is not None:
        knowledge_base.current_index_revision_id = replay.id
        knowledge_base.version += 1
        return replay
    revision = KnowledgeIndexRevision(
        id=uuid7(), tenant_id=knowledge_base.tenant_id,
        knowledge_base_id=knowledge_base.id,
        revision_no=knowledge_base.next_revision_no, status="ready",
        source_manifest=manifest, source_manifest_hash=manifest_hash,
        chunking_profile=dict(knowledge_base.chunking_profile),
        embedding_profile=dict(knowledge_base.embedding_profile),
        document_count=len(manifest), chunk_count=len(drafts),
        content_hash=content_hash, created_by_account_id=account_id,
    )
    db.add(revision)
    await db.flush()
    db.add_all([
        KnowledgeChunk(
            id=uuid7(), tenant_id=knowledge_base.tenant_id,
            index_revision_id=revision.id, knowledge_base_id=knowledge_base.id,
            document_id=document.id, source_id=source.id, ordinal=index,
            page_number=draft.page_number, section=draft.section,
            content=draft.content, content_hash=chunk_hashes[index - 1],
            term_frequencies=draft.terms, vector=draft.vector,
            token_count=draft.token_count,
        )
        for index, (document, source, draft) in enumerate(drafts, 1)
    ])
    knowledge_base.current_index_revision_id = revision.id
    knowledge_base.next_revision_no += 1
    knowledge_base.version += 1
    await db.flush()
    return revision


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(float(value) * float(right.get(key, 0.0)) for key, value in left.items())


def _lexical(query_terms: dict[str, int], document_terms: dict[str, int]) -> float:
    if not query_terms or not document_terms:
        return 0.0
    matched = sum(
        min(count, int(document_terms.get(term, 0)))
        for term, count in query_terms.items()
    )
    return matched / max(1.0, math.sqrt(
        sum(query_terms.values()) * sum(int(value) for value in document_terms.values())
    ))


async def search_knowledge(
    db, *, tenant_id: str, account_id: str, query: str,
    agent_id: str | None, knowledge_base_id: str | None = None,
    limit: int = 8, runner_id: str | None = None,
) -> dict:
    query = unicodedata.normalize("NFKC", query).strip()
    if not query or len(query) > 1000:
        raise KnowledgeError("knowledge_query_invalid", "A bounded search query is required")
    limit = max(1, min(int(limit), 20))
    statement = select(KnowledgeBase).where(
        KnowledgeBase.tenant_id == tenant_id,
        KnowledgeBase.owner_account_id == account_id,
        KnowledgeBase.status == "active",
        KnowledgeBase.current_index_revision_id.is_not(None),
    )
    if knowledge_base_id:
        statement = statement.where(KnowledgeBase.id == knowledge_base_id)
    if agent_id is not None:
        statement = statement.join(
            KnowledgeBaseAgentAccess,
            KnowledgeBaseAgentAccess.knowledge_base_id == KnowledgeBase.id,
        ).where(
            KnowledgeBase.sharing_policy == "selected_agents",
            KnowledgeBaseAgentAccess.tenant_id == tenant_id,
            KnowledgeBaseAgentAccess.agent_id == agent_id,
        )
    bases = list(await db.scalars(statement.order_by(KnowledgeBase.id)))
    if knowledge_base_id and not bases:
        raise KnowledgeError("knowledge_base_not_found", "Knowledge base not found", status_code=404)
    revision_ids = [item.current_index_revision_id for item in bases]
    chunks = list(await db.scalars(select(KnowledgeChunk).where(
        KnowledgeChunk.tenant_id == tenant_id,
        KnowledgeChunk.index_revision_id.in_(revision_ids),
    ))) if revision_ids else []
    sources = {
        item.id: item for item in await db.scalars(select(KnowledgeSource).where(
            KnowledgeSource.tenant_id == tenant_id,
            KnowledgeSource.id.in_({chunk.source_id for chunk in chunks}),
        ))
    } if chunks else {}
    bases_by_id = {item.id: item for item in bases}
    query_terms = dict(Counter(tokenize(query)))
    query_vector = feature_vector(query_terms)
    scored = []
    for chunk in chunks:
        lexical = _lexical(query_terms, chunk.term_frequencies or {})
        semantic = max(0.0, _cosine(query_vector, chunk.vector or {}))
        score = 0.55 * lexical + 0.45 * semantic
        if score <= 0:
            continue
        source = sources.get(chunk.source_id)
        base = bases_by_id.get(chunk.knowledge_base_id)
        if source is None or base is None or source.status == "deleted":
            continue
        scored.append((score, chunk, source, base, lexical, semantic))
    scored.sort(key=lambda item: (-item[0], item[1].id))
    results = []
    for score, chunk, source, base, lexical, semantic in scored[:limit]:
        results.append({
            "knowledge_base_id": base.id,
            "knowledge_base_name": base.name,
            "index_revision_id": chunk.index_revision_id,
            "document_id": chunk.document_id,
            "source_id": source.id,
            "chunk_id": chunk.id,
            "filename": source.filename,
            "page_number": chunk.page_number,
            "section": chunk.section,
            "source_checksum": source.checksum,
            "chunk_checksum": chunk.content_hash,
            "score": round(score, 6),
            "lexical_score": round(lexical, 6),
            "local_vector_score": round(semantic, 6),
            "excerpt": chunk.content[:1600],
            "citation": {
                "source_id": source.id,
                "index_revision_id": chunk.index_revision_id,
                "chunk_id": chunk.id,
                "filename": source.filename,
                "page_number": chunk.page_number,
                "section": chunk.section,
                "checksum": source.checksum,
            },
        })
    manifest = [{
        "knowledge_base_id": item.id,
        "index_revision_id": item.current_index_revision_id,
    } for item in bases]
    manifest_hash = _digest(manifest)
    query_hash = _digest(query.encode("utf-8"))
    if runner_id is not None:
        runner = await db.scalar(select(DurableRunner).where(
            DurableRunner.id == runner_id,
            DurableRunner.tenant_id == tenant_id,
            DurableRunner.agent_id == agent_id,
        ))
        if runner is None:
            raise KnowledgeError(
                "knowledge_runner_authority_invalid",
                "Knowledge retrieval Runner authority is invalid",
                status_code=403,
            )
        existing = await db.scalar(select(KnowledgeRetrievalSnapshot).where(
            KnowledgeRetrievalSnapshot.runner_id == runner_id,
            KnowledgeRetrievalSnapshot.query_hash == query_hash,
            KnowledgeRetrievalSnapshot.revision_manifest_hash == manifest_hash,
        ))
        if existing is None:
            snapshot_results = [{
                key: value for key, value in item.items()
                if key not in {"excerpt", "lexical_score", "local_vector_score"}
            } for item in results]
            db.add(KnowledgeRetrievalSnapshot(
                id=uuid7(), tenant_id=tenant_id, account_id=account_id,
                runner_id=runner_id, agent_id=agent_id,
                query_hash=query_hash, revision_manifest=manifest,
                revision_manifest_hash=manifest_hash,
                result_snapshot=snapshot_results,
                content_hash=_digest({
                    "query_hash": query_hash,
                    "revision_manifest": manifest,
                    "results": snapshot_results,
                }),
            ))
            await db.flush()
    return {
        "query": query,
        "count": len(results),
        "results": results,
        "revision_manifest": manifest,
        "retrieval_profile": {
            "hybrid": "local.term_frequency.v1+local.feature_hash.v1",
            "external_service": False,
        },
    }


async def delete_document(
    db, *, knowledge_base: KnowledgeBase, document_id: str, account_id: str,
) -> tuple[KnowledgeDocument, KnowledgeIndexRevision]:
    document = await db.scalar(select(KnowledgeDocument).where(
        KnowledgeDocument.id == document_id,
        KnowledgeDocument.tenant_id == knowledge_base.tenant_id,
        KnowledgeDocument.knowledge_base_id == knowledge_base.id,
    ).with_for_update())
    if document is None:
        raise KnowledgeError("knowledge_document_not_found", "Document not found", status_code=404)
    if document.status != "deleted":
        document.status = "deleted"
        document.deleted_at = utcnow()
        document.version += 1
        if document.current_source_id:
            source = await db.get(KnowledgeSource, document.current_source_id)
            if source is not None:
                source.status = "deleted"
    revision = await build_index_revision(
        db, knowledge_base=knowledge_base, account_id=account_id,
    )
    return document, revision


async def knowledge_base_view(db, item: KnowledgeBase) -> dict:
    revision = (
        await db.get(KnowledgeIndexRevision, item.current_index_revision_id)
        if item.current_index_revision_id else None
    )
    document_count = len(list(await db.scalars(select(KnowledgeDocument.id).where(
        KnowledgeDocument.tenant_id == item.tenant_id,
        KnowledgeDocument.knowledge_base_id == item.id,
        KnowledgeDocument.status == "active",
    ))))
    return {
        "id": item.id,
        "tenant_id": item.tenant_id,
        "owner_account_id": item.owner_account_id,
        "name": item.name,
        "description": item.description,
        "sharing_policy": item.sharing_policy,
        "status": item.status,
        "agent_ids": await agent_access_ids(db, item.id, item.tenant_id),
        "current_index_revision": index_revision_view(revision) if revision else None,
        "document_count": document_count,
        "chunking_profile": dict(item.chunking_profile or {}),
        "embedding_profile": dict(item.embedding_profile or {}),
        "version": item.version,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def index_revision_view(item: KnowledgeIndexRevision) -> dict:
    return {
        "id": item.id,
        "revision_no": item.revision_no,
        "status": item.status,
        "source_manifest_hash": item.source_manifest_hash,
        "document_count": item.document_count,
        "chunk_count": item.chunk_count,
        "content_hash": item.content_hash,
        "built_at": item.built_at.isoformat() if item.built_at else None,
    }


def source_view(item: KnowledgeSource) -> dict:
    return {
        "id": item.id,
        "document_id": item.document_id,
        "checksum": item.checksum,
        "filename": item.filename,
        "media_type": item.media_type,
        "size_bytes": item.size_bytes,
        "status": item.status,
        "parse_metadata": dict(item.parse_metadata or {}),
        "error_code": item.error_code,
        "error_detail": item.error_detail,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def document_view(item: KnowledgeDocument, source: KnowledgeSource | None) -> dict:
    return {
        "id": item.id,
        "knowledge_base_id": item.knowledge_base_id,
        "title": item.title,
        "logical_key": item.logical_key,
        "status": item.status,
        "current_source": source_view(source) if source else None,
        "version": item.version,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }
