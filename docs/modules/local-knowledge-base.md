# Local knowledge base

MyClaw `v0.3.0` adds a local, Tenant-scoped document knowledge system. It is
separate from Memory: Knowledge is imported source evidence that an Agent can
retrieve and cite; Memory is a curated fact about a user or Agent. Uploading a
document does not place it in chat context and does not grant every Agent
access.

## Ownership and access

Every `KnowledgeBase` belongs to one `tenant_id + owner_account_id`. It starts
as `owner_only`; the owner must explicitly grant selected Agents and include
the `knowledge_search` Tool in each AgentVersion that may retrieve it. Both
checks are required. A Group receives no implicit access: each participating
Agent uses its own grant under the originating account authority.

User APIs never accept a caller-supplied Tenant. Authentication supplies the
Tenant/account authority, and guessed knowledge, document, source or Agent IDs
from another boundary fail as missing resources. Composite MySQL foreign keys
prevent cross-Tenant relationships even if an application query regresses.

## Ingestion and immutable evidence

The first profile accepts bounded UTF-8 `.txt`, Markdown and text-extractable
PDF files. Parsing and local file I/O run outside the API event loop. Encrypted
PDFs, invalid UTF-8, files over the configured byte/page limits and documents
that would exceed the per-base chunk limit fail with stable error codes.

Source bytes are stored beneath `KNOWLEDGE_ROOT` in checksum-addressed paths.
They use a dedicated knowledge lifetime rather than a Session Workspace
lifetime. Writes use a temporary file, `fsync`, atomic replacement and a
checksum verification on every later load. The authoritative database records
the storage key, parser evidence, checksum and status; no mandatory cloud
storage or embedding service is used.

The data model deliberately separates:

- `KnowledgeDocument`: the stable logical filename and current source pointer;
- `KnowledgeSource`: an immutable uploaded body and parse result;
- `KnowledgeIndexRevision`: an immutable source manifest and indexed profile;
- `KnowledgeChunk`: a revision-bound excerpt, hashes and local retrieval data;
- `KnowledgeRetrievalSnapshot`: the exact revision/chunk references used by a
  Runner.

Uploading an already known checksum is idempotent. Uploading changed bytes under
the same filename creates a new source and index revision while retaining the
old evidence. Deleting a document creates a new current index without it and
tombstones the source for future retrieval; historical revisions, chunks and
Runner snapshots remain intact.

## Retrieval and citations

The local retrieval profile combines normalized term-frequency scoring with a
deterministic feature-hashing vector. It is small-node friendly and sends no
document text to an external embedding provider. Chunking and embedding
profiles are stored on every immutable index revision so a future profile
change is reproducible rather than silently rewriting the index.

Each result contains the KnowledgeBase, document/source/chunk IDs, exact index
revision, filename, page or Markdown section, source and chunk checksums, score
components and a bounded excerpt. A Tool search made during a Run stores the
query hash, revision manifest and result references in
`KnowledgeRetrievalSnapshot`; it does not duplicate the source excerpt into the
snapshot.

## Operator use

Open **Knowledge** in the sidebar to create a base, grant Agents, upload or
replace documents, inspect checksums and revision counts, run a direct local
search, rebuild the current index, reconcile database metadata with local
source bodies, or tombstone documents and bases. Reconciliation reports missing,
checksum-mismatched and unreferenced bodies. In repair mode a missing/corrupt
current source is failed closed and a new index revision excludes it; restoring
the exact checksum can safely re-admit it. Unreferenced files are reported but
never deleted automatically. The page
also reminds operators that both the explicit grant and AgentVersion Tool
selection are necessary.

The local source root must be included in backup/restore together with MySQL.
Missing bodies and checksum mismatches fail closed during retrieval and during
operator reconciliation. OCR, remote object storage, cross-account sharing,
automatic ingestion pipelines and scheduled background reconciliation are
deliberately deferred.
