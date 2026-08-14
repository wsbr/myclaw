"""Managed document ingestion, immutable indexing, retrieval, and citations."""

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
from app.database import AsyncSessionLocal
from app.models.agent_v1 import AgentBlueprint
from app.models.common import utcnow, uuid7
from app.models.knowledge_v1 import (
    KnowledgeBase,
    KnowledgeBaseAgentAccess,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIndexRevision,
    KnowledgeLexiconRevision,
    KnowledgeRetrievalSnapshot,
    KnowledgeSource,
)
from app.models.runtime_v1 import DurableRunner
from app.models.registry import ModelVersion, RegisteredModel
from app.persistence.reliable_writes import OutboxWriter
from app.services.embedding_service import (
    EmbeddingProviderError,
    EmbeddingSpec,
    OpenAICompatibleEmbeddingClient,
    embedding_spec,
    is_embedding_model,
)
from app.services.model_registry_service import latest_terminal_conformance
from app.services.provider_credentials import resolve_provider_credential
from app.services.sparse_retrieval_service import (
    BM25_B,
    BM25_K1,
    BM25_TOKENIZER_PROFILE,
    RRF_K,
    RRF_PROFILE,
    RankedRouteHit,
    bm25_query_vector,
    lexicon_vector,
    match_lexicon,
    normalize_lexicon_terms,
    reciprocal_rank_fusion,
    tokenize_for_bm25,
)
from app.services.vector_store_service import QdrantVectorStore, VectorStoreError


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
    return tokenize_for_bm25(text)


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
                "Existing managed source body has an invalid checksum",
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
    agent_ids: list[str], embedding_model_id: str | None = None,
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
    embedding_model = None
    embedding_version = None
    if embedding_model_id:
        embedding_model, embedding_version = await resolve_knowledge_embedding_model(
            db, embedding_model_id=embedding_model_id,
        )
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
        embedding_model_id=embedding_model.id if embedding_model else None,
        embedding_profile=(
            {
                "profile": "semantic.openai_compatible.v1",
                "model_id": embedding_model.id,
                "dimensions": embedding_spec(embedding_version).dimensions,
                "vector_backend": settings.knowledge_vector_backend,
            }
            if embedding_model and embedding_version
            else dict(DEFAULT_EMBEDDING_PROFILE)
        ),
    )
    db.add(item)
    await db.flush()
    await replace_agent_access(
        db, knowledge_base=item, account_id=account_id, agent_ids=agent_ids,
        bump_version=False,
    )
    return item


async def resolve_knowledge_embedding_model(
    db, *, embedding_model_id: str,
) -> tuple[RegisteredModel, ModelVersion]:
    model = await db.get(RegisteredModel, embedding_model_id)
    version = (
        await db.get(ModelVersion, model.current_version_id)
        if model is not None and model.current_version_id else None
    )
    if (
        model is None or model.status != "active" or not model.enabled
        or version is None or not is_embedding_model(version)
    ):
        raise KnowledgeError(
            "knowledge_embedding_model_unavailable",
            "Embedding model is unavailable or not activated",
            status_code=409,
        )
    conformance = await latest_terminal_conformance(db, version.id)
    if conformance is None or conformance.status != "passed":
        raise KnowledgeError(
            "knowledge_embedding_model_unverified",
            "Embedding model has not passed its conformance suite",
            status_code=409,
        )
    embedding_spec(version)
    return model, version


def _hybrid_retrieval_profile(
    *, lexicon_revision: KnowledgeLexiconRevision | None,
) -> dict:
    return {
        "profile": "qdrant.hybrid.v1",
        "routes": {
            "dense": {
                "vector_name": "dense",
                "candidate_limit": settings.knowledge_dense_candidate_limit,
            },
            "bm25": {
                "vector_name": "bm25",
                "candidate_limit": settings.knowledge_bm25_candidate_limit,
                "tokenizer": BM25_TOKENIZER_PROFILE,
                "k1": BM25_K1,
                "b": BM25_B,
                "idf": "myclaw.revision_frozen.v1",
            },
            "lexicon": {
                "vector_name": "lexicon",
                "candidate_limit": settings.knowledge_lexicon_candidate_limit,
                "revision_id": lexicon_revision.id if lexicon_revision else None,
                "revision_no": lexicon_revision.revision_no if lexicon_revision else 0,
                "content_hash": (
                    lexicon_revision.content_hash if lexicon_revision else _digest([])
                ),
                "term_count": len(lexicon_revision.terms) if lexicon_revision else 0,
            },
        },
        "fusion": {"profile": RRF_PROFILE, "k": RRF_K},
        "reranker": {"enabled": False, "fallback": "rrf"},
    }


def lexicon_revision_view(item: KnowledgeLexiconRevision | None) -> dict:
    if item is None:
        return {
            "id": None, "revision_no": 0, "content_hash": _digest([]),
            "terms": [], "created_at": None,
        }
    return {
        "id": item.id,
        "revision_no": item.revision_no,
        "content_hash": item.content_hash,
        "terms": [{
            "id": term["id"],
            "canonical_name": term["canonical_name"],
            "term_type": term["term_type"],
            "description": term.get("description", ""),
            "aliases": [alias["value"] for alias in term.get("aliases", [])],
        } for term in item.terms],
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


async def current_knowledge_lexicon(db, knowledge_base: KnowledgeBase) -> KnowledgeLexiconRevision | None:
    if not knowledge_base.current_lexicon_revision_id:
        return None
    lexicon = await db.get(
        KnowledgeLexiconRevision, knowledge_base.current_lexicon_revision_id,
    )
    if (
        lexicon is None or lexicon.tenant_id != knowledge_base.tenant_id
        or lexicon.knowledge_base_id != knowledge_base.id
    ):
        raise KnowledgeError(
            "knowledge_lexicon_authority_invalid",
            "Knowledge lexicon revision authority is invalid",
            status_code=500,
        )
    return lexicon


async def replace_knowledge_lexicon(
    db, *, knowledge_base: KnowledgeBase, account_id: str, raw_terms: list[dict],
) -> tuple[KnowledgeLexiconRevision, KnowledgeIndexRevision | None, bool]:
    if len(raw_terms) > settings.knowledge_lexicon_max_terms:
        raise KnowledgeError(
            "knowledge_lexicon_term_limit",
            f"A lexicon may contain at most {settings.knowledge_lexicon_max_terms} terms",
        )
    for raw in raw_terms:
        aliases = raw.get("aliases") or []
        if len(aliases) > settings.knowledge_lexicon_max_aliases_per_term:
            raise KnowledgeError(
                "knowledge_lexicon_alias_limit",
                "A lexicon term has too many aliases",
            )
        if str(raw.get("term_type") or "concept").casefold() not in {
            "concept", "entity", "code", "product", "agent", "other",
        }:
            raise KnowledgeError(
                "knowledge_lexicon_term_type_invalid", "Lexicon term type is invalid",
            )
        if len(str(raw.get("description") or "")) > 2000:
            raise KnowledgeError(
                "knowledge_lexicon_description_invalid",
                "Lexicon term description exceeds 2000 characters",
            )
    try:
        terms = normalize_lexicon_terms(raw_terms)
    except ValueError as error:
        raise KnowledgeError("knowledge_lexicon_invalid", str(error)) from error
    content_hash = _digest(terms)
    current = await current_knowledge_lexicon(db, knowledge_base)
    if current is not None and current.content_hash == content_hash:
        return current, None, True
    revision = await db.scalar(select(KnowledgeLexiconRevision).where(
        KnowledgeLexiconRevision.knowledge_base_id == knowledge_base.id,
        KnowledgeLexiconRevision.tenant_id == knowledge_base.tenant_id,
        KnowledgeLexiconRevision.content_hash == content_hash,
    ).with_for_update())
    if revision is None:
        revision = KnowledgeLexiconRevision(
            id=uuid7(), tenant_id=knowledge_base.tenant_id,
            knowledge_base_id=knowledge_base.id,
            revision_no=knowledge_base.next_lexicon_revision_no,
            terms=terms, content_hash=content_hash,
            created_by_account_id=account_id,
        )
        db.add(revision)
        await db.flush([revision])
        knowledge_base.next_lexicon_revision_no += 1
    knowledge_base.current_lexicon_revision_id = revision.id
    knowledge_base.version += 1
    await db.flush()
    index_revision = None
    if knowledge_base.embedding_model_id:
        index_revision = await schedule_semantic_index_revision(
            db, knowledge_base=knowledge_base, account_id=account_id,
        )
    return revision, index_revision, False


async def configure_knowledge_embedding(
    db, *, knowledge_base: KnowledgeBase, account_id: str,
    embedding_model_id: str,
) -> KnowledgeIndexRevision:
    model, version = await resolve_knowledge_embedding_model(
        db, embedding_model_id=embedding_model_id,
    )
    spec = embedding_spec(version)
    knowledge_base.embedding_model_id = model.id
    knowledge_base.embedding_profile = {
        "profile": "semantic.openai_compatible.v1",
        "model_id": model.id,
        "dimensions": spec.dimensions,
        "vector_backend": settings.knowledge_vector_backend,
    }
    knowledge_base.version += 1
    await db.flush()
    return await schedule_semantic_index_revision(
        db, knowledge_base=knowledge_base, account_id=account_id,
    )


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


async def schedule_semantic_index_revision(
    db, *, knowledge_base: KnowledgeBase, account_id: str,
    force_projection_rebuild: bool = False,
) -> KnowledgeIndexRevision:
    if not knowledge_base.embedding_model_id:
        raise KnowledgeError(
            "knowledge_embedding_model_missing", "Knowledge base has no Embedding model",
            status_code=409,
        )
    _, version = await resolve_knowledge_embedding_model(
        db, embedding_model_id=knowledge_base.embedding_model_id,
    )
    spec = embedding_spec(version)
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
    profile = spec.snapshot()
    lexicon_revision = await current_knowledge_lexicon(db, knowledge_base)
    retrieval_profile = _hybrid_retrieval_profile(
        lexicon_revision=lexicon_revision,
    )
    manifest_hash = _digest({
        "sources": manifest,
        "chunking_profile": knowledge_base.chunking_profile,
        "embedding_profile": profile,
        "retrieval_profile": retrieval_profile,
        "vector_backend": settings.knowledge_vector_backend,
    })
    content_hash = _digest({
        "knowledge_base_id": knowledge_base.id,
        "source_manifest_hash": manifest_hash,
        # A manual rebuild deliberately creates a new immutable revision even
        # when logical source/model inputs are unchanged. This is the recovery
        # path after a vector-store restore or projection integrity incident.
        "projection_rebuild_generation": uuid7() if force_projection_rebuild else None,
    })
    existing = await db.scalar(select(KnowledgeIndexRevision).where(
        KnowledgeIndexRevision.knowledge_base_id == knowledge_base.id,
        KnowledgeIndexRevision.content_hash == content_hash,
    ).with_for_update())
    if existing is not None:
        if existing.status == "ready":
            knowledge_base.current_index_revision_id = existing.id
            knowledge_base.pending_index_revision_id = None
            knowledge_base.version += 1
            return existing
        if existing.status in {"queued", "running"}:
            knowledge_base.pending_index_revision_id = existing.id
            return existing
        existing.status = "queued"
        existing.error_code = None
        existing.error_detail = None
        existing.started_at = None
        existing.completed_at = None
        revision = existing
        replay_generation = uuid7()
    else:
        revision = KnowledgeIndexRevision(
            id=uuid7(), tenant_id=knowledge_base.tenant_id,
            knowledge_base_id=knowledge_base.id,
            revision_no=knowledge_base.next_revision_no,
            status="queued", source_manifest=manifest,
            source_manifest_hash=manifest_hash,
            chunking_profile=dict(knowledge_base.chunking_profile),
            embedding_profile=profile,
            embedding_model_version_id=version.id,
            lexicon_revision_id=(lexicon_revision.id if lexicon_revision else None),
            retrieval_profile=retrieval_profile,
            vector_backend=settings.knowledge_vector_backend,
            vector_collection=None, vector_point_count=0,
            document_count=len(manifest), chunk_count=0,
            content_hash=content_hash,
            created_by_account_id=account_id,
        )
        db.add(revision)
        # There is an intentional circular reference between KnowledgeBase
        # and its immutable revisions. Persist the child revision before
        # moving the pending pointer so MySQL never observes a dangling FK.
        await db.flush([revision])
        knowledge_base.next_revision_no += 1
        replay_generation = "initial"
    knowledge_base.pending_index_revision_id = revision.id
    knowledge_base.version += 1
    await db.flush()
    await OutboxWriter(db).enqueue(
        topic="knowledge.index", action="build_semantic_index",
        aggregate_type="knowledge_index_revision", aggregate_id=revision.id,
        partition_key=knowledge_base.id,
        dedupe_key=f"semantic-index:{revision.id}:{replay_generation}",
        tenant_id=knowledge_base.tenant_id, account_id=account_id,
        payload_ref={
            "knowledge_base_id": knowledge_base.id,
            "index_revision_id": revision.id,
            "embedding_model_version_id": version.id,
        },
        priority=80,
    )
    return revision


async def build_index_revision(
    db, *, knowledge_base: KnowledgeBase, account_id: str,
    force_semantic_rebuild: bool = False,
) -> KnowledgeIndexRevision:
    if knowledge_base.embedding_model_id:
        return await schedule_semantic_index_revision(
            db, knowledge_base=knowledge_base, account_id=account_id,
            force_projection_rebuild=force_semantic_rebuild,
        )
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
        embedding_model_version_id=None,
        vector_backend="mysql_json", vector_collection=None,
        vector_point_count=len(drafts),
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
    knowledge_base.pending_index_revision_id = None
    knowledge_base.next_revision_no += 1
    knowledge_base.version += 1
    await db.flush()
    return revision


async def reconcile_knowledge_storage(
    db, *, knowledge_base: KnowledgeBase, account_id: str, repair: bool = True,
) -> dict:
    """Compare authoritative source metadata with local bodies and fail closed.

    Historical source rows are never removed. When a current indexed body is
    missing or corrupt, repair mode marks only that mutable availability state
    failed and creates a new immutable index revision excluding it. A body that
    later becomes valid can be restored to the current index without rewriting
    the earlier evidence.
    """

    sources = list(await db.scalars(select(KnowledgeSource).where(
        KnowledgeSource.tenant_id == knowledge_base.tenant_id,
        KnowledgeSource.knowledge_base_id == knowledge_base.id,
    ).order_by(KnowledgeSource.created_at, KnowledgeSource.id)))
    documents = list(await db.scalars(select(KnowledgeDocument).where(
        KnowledgeDocument.tenant_id == knowledge_base.tenant_id,
        KnowledgeDocument.knowledge_base_id == knowledge_base.id,
    )))
    current_source_ids = {
        item.current_source_id for item in documents
        if item.status == "active" and item.current_source_id
    }
    expected_keys = {item.storage_key for item in sources}
    missing: list[dict] = []
    corrupt: list[dict] = []
    restored: list[str] = []
    changed: list[str] = []
    reconciliation_codes = {
        "knowledge_source_body_missing",
        "knowledge_source_checksum_mismatch",
    }
    for source in sources:
        error: KnowledgeError | None = None
        try:
            await asyncio.to_thread(load_source_bytes, source)
        except KnowledgeError as caught:
            error = caught
            target = missing if caught.code == "knowledge_source_body_missing" else corrupt
            target.append({
                "source_id": source.id,
                "storage_key": source.storage_key,
                "code": caught.code,
                "current": source.id in current_source_ids,
            })
        if not repair or source.id not in current_source_ids:
            continue
        if error is not None and source.status == "indexed":
            source.status = "failed"
            source.error_code = error.code
            source.error_detail = str(error)
            changed.append(source.id)
        elif (
            error is None
            and source.status == "failed"
            and source.error_code in reconciliation_codes
        ):
            source.status = "indexed"
            source.error_code = None
            source.error_detail = None
            restored.append(source.id)
            changed.append(source.id)

    prefix = (
        Path(settings.knowledge_root).resolve()
        / knowledge_base.tenant_id / knowledge_base.id
    )
    orphaned_bodies: list[str] = []
    if prefix.exists():
        for target in await asyncio.to_thread(lambda: list(prefix.rglob("*"))):
            if not target.is_file() or target.name.endswith(".tmp"):
                continue
            # Report the lexical in-root path without following a possible
            # operator-created symlink outside KNOWLEDGE_ROOT.
            storage_key = target.relative_to(prefix.parent.parent).as_posix()
            if storage_key not in expected_keys:
                orphaned_bodies.append(storage_key)
    orphaned_bodies.sort()

    revision = None
    if repair and changed:
        await db.flush()
        revision = await build_index_revision(
            db, knowledge_base=knowledge_base, account_id=account_id,
        )
    return {
        "knowledge_base_id": knowledge_base.id,
        "checked_source_count": len(sources),
        "missing_sources": missing,
        "corrupt_sources": corrupt,
        "orphaned_bodies": orphaned_bodies,
        "changed_source_ids": changed,
        "restored_source_ids": restored,
        "repair_applied": bool(repair),
        "index_revision": index_revision_view(revision) if revision else None,
        "healthy": not missing and not corrupt and not orphaned_bodies,
    }


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


@dataclass(frozen=True)
class SemanticSearchGroup:
    spec: EmbeddingSpec
    credential: str | None
    dense_prepare_error: str | None
    collection: str
    revision_ids: tuple[str, ...]
    retrieval_profile: dict
    lexicon_terms: tuple[dict, ...]


@dataclass(frozen=True)
class DetachedSearchPlan:
    tenant_id: str
    account_id: str
    agent_id: str | None
    runner_id: str | None
    query: str
    query_terms: dict[str, int]
    limit: int
    bases: tuple[dict, ...]
    local_candidates: tuple[tuple[str, float, float], ...]
    semantic_groups: tuple[SemanticSearchGroup, ...]


_retrieval_embedding_client: OpenAICompatibleEmbeddingClient | None = None
_retrieval_vector_store: QdrantVectorStore | None = None


def _shared_retrieval_clients() -> tuple[
    OpenAICompatibleEmbeddingClient, QdrantVectorStore,
]:
    global _retrieval_embedding_client, _retrieval_vector_store
    if _retrieval_embedding_client is None:
        _retrieval_embedding_client = OpenAICompatibleEmbeddingClient(
            timeout_seconds=settings.knowledge_embedding_timeout_seconds,
        )
    if _retrieval_vector_store is None:
        _retrieval_vector_store = QdrantVectorStore()
    return _retrieval_embedding_client, _retrieval_vector_store


async def close_knowledge_retrieval_clients() -> None:
    """Close process-wide HTTP pools during the application lifespan shutdown."""

    global _retrieval_embedding_client, _retrieval_vector_store
    clients = tuple(item for item in (
        _retrieval_embedding_client, _retrieval_vector_store,
    ) if item is not None)
    _retrieval_embedding_client = None
    _retrieval_vector_store = None
    if clients:
        await asyncio.gather(
            *(item.close() for item in clients), return_exceptions=True,
        )


async def _accessible_bases(
    db, *, tenant_id: str, account_id: str, agent_id: str | None,
    knowledge_base_id: str | None,
) -> list[KnowledgeBase]:
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
        raise KnowledgeError(
            "knowledge_base_not_found", "Knowledge base not found", status_code=404,
        )
    return bases


async def prepare_detached_search(
    *, tenant_id: str, account_id: str, query: str,
    agent_id: str | None, knowledge_base_id: str | None,
    limit: int, runner_id: str | None,
) -> DetachedSearchPlan:
    query = unicodedata.normalize("NFKC", query).strip()
    if not query or len(query) > 1000:
        raise KnowledgeError("knowledge_query_invalid", "A bounded search query is required")
    limit = max(1, min(int(limit), 20))
    query_terms = dict(Counter(tokenize(query)))
    query_vector = feature_vector(query_terms)
    async with AsyncSessionLocal() as db:
        bases = await _accessible_bases(
            db, tenant_id=tenant_id, account_id=account_id,
            agent_id=agent_id, knowledge_base_id=knowledge_base_id,
        )
        revisions = {
            item.id: item for item in await db.scalars(select(KnowledgeIndexRevision).where(
                KnowledgeIndexRevision.id.in_([
                    item.current_index_revision_id for item in bases
                ]),
                KnowledgeIndexRevision.status == "ready",
            ))
        } if bases else {}
        base_snapshots = tuple({
            "id": item.id,
            "name": item.name,
            "index_revision_id": item.current_index_revision_id,
            "retrieval_profile": dict(
                revisions[item.current_index_revision_id].retrieval_profile or {}
            ),
            "lexicon_revision_id": revisions[
                item.current_index_revision_id
            ].lexicon_revision_id,
        } for item in bases if item.current_index_revision_id in revisions)
        local_revision_ids = [
            revision.id for revision in revisions.values()
            if revision.vector_backend != "qdrant"
        ]
        local_candidates: list[tuple[str, float, float]] = []
        if local_revision_ids:
            chunks = list(await db.scalars(select(KnowledgeChunk).where(
                KnowledgeChunk.tenant_id == tenant_id,
                KnowledgeChunk.index_revision_id.in_(local_revision_ids),
            )))
            for chunk in chunks:
                lexical = _lexical(query_terms, chunk.term_frequencies or {})
                local_vector = max(0.0, _cosine(query_vector, chunk.vector or {}))
                score = 0.55 * lexical + 0.45 * local_vector
                if score > 0:
                    local_candidates.append((chunk.id, score, local_vector))
            local_candidates.sort(key=lambda item: (-item[1], item[0]))
            local_candidates = local_candidates[
                :limit * settings.knowledge_vector_candidate_multiplier
            ]
        semantic_revision_groups: dict[tuple[str, str, str | None], list[str]] = {}
        for revision in revisions.values():
            if (
                revision.vector_backend == "qdrant"
                and revision.vector_collection
                and revision.embedding_model_version_id
            ):
                semantic_revision_groups.setdefault(
                    (
                        revision.embedding_model_version_id,
                        revision.vector_collection,
                        revision.lexicon_revision_id,
                    ), [],
                ).append(revision.id)
        semantic_groups: list[SemanticSearchGroup] = []
        for (
            model_version_id, collection, lexicon_revision_id
        ), revision_ids in semantic_revision_groups.items():
            version = await db.get(ModelVersion, model_version_id)
            if version is None:
                raise KnowledgeError(
                    "knowledge_embedding_model_version_missing",
                    "Indexed Embedding model evidence is unavailable",
                    status_code=503,
                )
            lexicon = (
                await db.get(KnowledgeLexiconRevision, lexicon_revision_id)
                if lexicon_revision_id else None
            )
            sample_revision = revisions[revision_ids[0]]
            credential = None
            dense_prepare_error = None
            try:
                credential = await resolve_provider_credential(
                    db, version.credential_ref,
                )
            except Exception as error:
                dense_prepare_error = str(
                    getattr(error, "code", type(error).__name__)
                )[:100]
            semantic_groups.append(SemanticSearchGroup(
                spec=embedding_spec(version), credential=credential,
                dense_prepare_error=dense_prepare_error,
                collection=collection,
                revision_ids=tuple(sorted(revision_ids)),
                retrieval_profile=dict(sample_revision.retrieval_profile or {}),
                lexicon_terms=tuple(
                    dict(item) for item in (lexicon.terms if lexicon else [])
                ),
            ))
        return DetachedSearchPlan(
            tenant_id=tenant_id, account_id=account_id,
            agent_id=agent_id, runner_id=runner_id,
            query=query, query_terms=query_terms, limit=limit,
            bases=base_snapshots,
            local_candidates=tuple(local_candidates),
            semantic_groups=tuple(semantic_groups),
        )


async def execute_detached_search(plan: DetachedSearchPlan) -> dict:
    route_scores: dict[str, dict[str, float]] = {}
    degraded_routes: list[dict] = []
    skipped_routes: list[dict] = []
    query_lexicon_matches: dict[str, dict] = {}
    hybrid_enabled = any(
        group.retrieval_profile.get("profile") == "qdrant.hybrid.v1"
        for group in plan.semantic_groups
    )
    embedding_client, vector_store = _shared_retrieval_clients()
    for group in plan.semantic_groups:
        hybrid = group.retrieval_profile.get("profile") == "qdrant.hybrid.v1"

        async def dense_route():
            if group.credential is None:
                return group.dense_prepare_error or "embedding_credential_unavailable"
            try:
                embedded = await embedding_client.embed(
                    spec=group.spec, api_key=group.credential, texts=[plan.query],
                )
                return await (
                    vector_store.query_dense(
                        collection=group.collection,
                        vector=embedded.vectors[0], tenant_id=plan.tenant_id,
                        revision_ids=list(group.revision_ids),
                        limit=settings.knowledge_dense_candidate_limit,
                    ) if hybrid else vector_store.query(
                        collection=group.collection,
                        vector=embedded.vectors[0], tenant_id=plan.tenant_id,
                        revision_ids=list(group.revision_ids),
                        limit=plan.limit * settings.knowledge_vector_candidate_multiplier,
                    )
                )
            except EmbeddingProviderError as error:
                degraded_routes.append({
                    "route": "dense", "code": error.code,
                    "retryable": error.retryable,
                })
                return None

        tasks: dict[str, asyncio.Task] = {
            "dense": asyncio.create_task(dense_route()),
        }
        lexicon_matches = match_lexicon(plan.query, group.lexicon_terms)
        for item in lexicon_matches:
            query_lexicon_matches[item.term_id] = {
                "term_id": item.term_id,
                "canonical_name": item.canonical_name,
                "matched_value": item.matched_value,
            }
        if hybrid:
            tasks["bm25"] = asyncio.create_task(vector_store.query_sparse(
                collection=group.collection,
                vector=bm25_query_vector(plan.query), using="bm25",
                tenant_id=plan.tenant_id,
                revision_ids=list(group.revision_ids),
                limit=settings.knowledge_bm25_candidate_limit,
            ))
            if lexicon_matches:
                tasks["lexicon"] = asyncio.create_task(vector_store.query_sparse(
                    collection=group.collection,
                    vector=lexicon_vector(lexicon_matches), using="lexicon",
                    tenant_id=plan.tenant_id,
                    revision_ids=list(group.revision_ids),
                    limit=settings.knowledge_lexicon_candidate_limit,
                ))
            else:
                skipped_routes.append({
                    "route": "lexicon", "reason": "no_query_term_match",
                    "revision_ids": list(group.revision_ids),
                })
        for route_name, task in tasks.items():
            try:
                matches = await task
            except VectorStoreError as error:
                for pending in tasks.values():
                    if not pending.done():
                        pending.cancel()
                await asyncio.gather(*tasks.values(), return_exceptions=True)
                raise KnowledgeError(
                    error.code,
                    "Knowledge vector storage is temporarily unavailable"
                    if error.retryable else "Knowledge vector storage configuration is invalid",
                    status_code=503,
                ) from error
            if isinstance(matches, str):
                degraded_routes.append({
                    "route": "dense", "code": matches, "retryable": False,
                })
                continue
            if matches is None:
                continue
            bucket = route_scores.setdefault(route_name, {})
            for item in matches:
                if item.score > 0:
                    bucket[item.id] = max(
                        bucket.get(item.id, float("-inf")), item.score,
                    )

    local_by_id = {
        chunk_id: (score, local_vector)
        for chunk_id, score, local_vector in plan.local_candidates
    }
    if local_by_id:
        route_scores["legacy_local"] = {
            chunk_id: score for chunk_id, (score, _) in local_by_id.items()
        }
    ranked_routes = {
        route_name: [
            RankedRouteHit(id=chunk_id, score=score)
            for chunk_id, score in sorted(
                scores.items(), key=lambda item: (-item[1], item[0]),
            )
        ]
        for route_name, scores in route_scores.items()
    }
    fused_hits = reciprocal_rank_fusion(ranked_routes, k=RRF_K)
    fused_by_id = {item.id: item for item in fused_hits}
    candidate_ids = set(fused_by_id)
    planned_revision_ids = {item["index_revision_id"] for item in plan.bases}
    planned_base_by_id = {item["id"]: item for item in plan.bases}
    async with AsyncSessionLocal.begin() as db:
        current_bases = await _accessible_bases(
            db, tenant_id=plan.tenant_id, account_id=plan.account_id,
            agent_id=plan.agent_id, knowledge_base_id=None,
        )
        valid_base_ids = {
            item.id for item in current_bases
            if planned_base_by_id.get(item.id, {}).get("index_revision_id")
            == item.current_index_revision_id
        }
        chunks = list(await db.scalars(select(KnowledgeChunk).where(
            KnowledgeChunk.id.in_(candidate_ids),
            KnowledgeChunk.tenant_id == plan.tenant_id,
            KnowledgeChunk.index_revision_id.in_(planned_revision_ids),
            KnowledgeChunk.knowledge_base_id.in_(valid_base_ids),
        ))) if candidate_ids and valid_base_ids else []
        sources = {
            item.id: item for item in await db.scalars(select(KnowledgeSource).where(
                KnowledgeSource.tenant_id == plan.tenant_id,
                KnowledgeSource.id.in_({chunk.source_id for chunk in chunks}),
            ))
        } if chunks else {}
        scored = []
        availability: dict[str, bool] = {}
        for chunk in chunks:
            source = sources.get(chunk.source_id)
            base = planned_base_by_id.get(chunk.knowledge_base_id)
            if source is None or base is None or source.status == "deleted":
                continue
            if source.id not in availability:
                try:
                    await asyncio.to_thread(load_source_bytes, source)
                    availability[source.id] = True
                except KnowledgeError:
                    availability[source.id] = False
            if not availability[source.id]:
                continue
            fused = fused_by_id[chunk.id]
            matched_terms = [
                query_lexicon_matches[term_id]
                for term_id in (chunk.lexicon_term_ids or [])
                if term_id in query_lexicon_matches
            ]
            scored.append((
                fused.score, chunk, source, base, fused.routes, matched_terms,
            ))
        scored.sort(key=lambda item: (-item[0], item[1].id))
        results = []
        for score, chunk, source, base, routes, matched_terms in scored[:plan.limit]:
            dense_score = float(routes.get("dense", {}).get("score", 0.0))
            lexical_score = float(routes.get("bm25", {}).get("score", 0.0))
            retrieval_kind = (
                "hybrid_rrf" if base["retrieval_profile"].get("profile")
                == "qdrant.hybrid.v1" else "semantic_legacy"
            )
            results.append({
                "knowledge_base_id": base["id"],
                "knowledge_base_name": base["name"],
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
                "rrf_score": round(score, 10),
                "lexical_score": round(lexical_score, 6),
                "semantic_score": round(dense_score, 6),
                "retrieval_kind": retrieval_kind,
                "route_evidence": routes,
                "matched_lexicon_terms": matched_terms,
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
            "knowledge_base_id": item["id"],
            "index_revision_id": item["index_revision_id"],
            "retrieval_profile": item["retrieval_profile"].get("profile") or "legacy",
            "lexicon_revision_id": item["lexicon_revision_id"],
        } for item in plan.bases if item["id"] in valid_base_ids]
        manifest_hash = _digest(manifest)
        query_hash = _digest(plan.query.encode("utf-8"))
        if plan.runner_id is not None:
            runner = await db.scalar(select(DurableRunner).where(
                DurableRunner.id == plan.runner_id,
                DurableRunner.tenant_id == plan.tenant_id,
                DurableRunner.agent_id == plan.agent_id,
            ))
            if runner is None:
                raise KnowledgeError(
                    "knowledge_runner_authority_invalid",
                    "Knowledge retrieval Runner authority is invalid",
                    status_code=403,
                )
            existing = await db.scalar(select(KnowledgeRetrievalSnapshot).where(
                KnowledgeRetrievalSnapshot.runner_id == plan.runner_id,
                KnowledgeRetrievalSnapshot.query_hash == query_hash,
                KnowledgeRetrievalSnapshot.revision_manifest_hash == manifest_hash,
            ))
            if existing is None:
                snapshot_results = [{
                    key: value for key, value in item.items()
                    if key != "excerpt"
                } for item in results]
                db.add(KnowledgeRetrievalSnapshot(
                    id=uuid7(), tenant_id=plan.tenant_id,
                    account_id=plan.account_id, runner_id=plan.runner_id,
                    agent_id=plan.agent_id, query_hash=query_hash,
                    revision_manifest=manifest,
                    revision_manifest_hash=manifest_hash,
                    result_snapshot=snapshot_results,
                    retrieval_profile={
                        "profile": (
                            "qdrant.hybrid.v1" if hybrid_enabled
                            else "legacy.hybrid.v1"
                        ),
                        "fusion": {"profile": RRF_PROFILE, "k": RRF_K},
                        "routes": sorted(route_scores),
                    },
                    degraded_routes=degraded_routes,
                    content_hash=_digest({
                        "query_hash": query_hash,
                        "revision_manifest": manifest,
                        "results": snapshot_results,
                        "degraded_routes": degraded_routes,
                    }),
                ))
        return {
            "query": plan.query,
            "count": len(results),
            "results": results,
            "revision_manifest": manifest,
            "degraded_routes": degraded_routes,
            "skipped_routes": skipped_routes,
            "retrieval_profile": {
                "profile": (
                    "qdrant.hybrid.v1" if hybrid_enabled
                    else "semantic.embedding.v1+local.term_frequency.v1"
                ),
                "vector_backend": "qdrant" if plan.semantic_groups else "mysql_json",
                "external_embedding": bool(plan.semantic_groups),
                "routes": sorted(route_scores),
                "fusion": {"profile": RRF_PROFILE, "k": RRF_K},
                "reranker": {"enabled": False, "fallback": "rrf"},
            },
        }


async def search_knowledge_detached(
    *, tenant_id: str, account_id: str, query: str,
    agent_id: str | None, knowledge_base_id: str | None = None,
    limit: int = 8, runner_id: str | None = None,
) -> dict:
    plan = await prepare_detached_search(
        tenant_id=tenant_id, account_id=account_id,
        query=query, agent_id=agent_id,
        knowledge_base_id=knowledge_base_id,
        limit=limit, runner_id=runner_id,
    )
    return await execute_detached_search(plan)


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
    available_sources: dict[str, bool] = {}
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
        if source.id not in available_sources:
            try:
                await asyncio.to_thread(load_source_bytes, source)
                available_sources[source.id] = True
            except KnowledgeError:
                available_sources[source.id] = False
        if not available_sources[source.id]:
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
    pending_revision = (
        await db.get(KnowledgeIndexRevision, item.pending_index_revision_id)
        if item.pending_index_revision_id else None
    )
    lexicon_revision = await current_knowledge_lexicon(db, item)
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
        "pending_index_revision": (
            index_revision_view(pending_revision) if pending_revision else None
        ),
        "document_count": document_count,
        "chunking_profile": dict(item.chunking_profile or {}),
        "embedding_profile": dict(item.embedding_profile or {}),
        "embedding_model_id": item.embedding_model_id,
        "current_lexicon_revision": lexicon_revision_view(lexicon_revision),
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
        "embedding_model_version_id": item.embedding_model_version_id,
        "embedding_profile": dict(item.embedding_profile or {}),
        "lexicon_revision_id": item.lexicon_revision_id,
        "retrieval_profile": dict(item.retrieval_profile or {}),
        "vector_backend": item.vector_backend,
        "vector_collection": item.vector_collection,
        "vector_point_count": item.vector_point_count,
        "error_code": item.error_code,
        "error_detail": item.error_detail,
        "content_hash": item.content_hash,
        "built_at": item.built_at.isoformat() if item.built_at else None,
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
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
