# Semantic knowledge base

MyClaw `v0.4.1` adds real three-route retrieval and Qdrant vector projection to
the Tenant-scoped knowledge system. Knowledge remains separate from Memory:
Knowledge is imported source evidence an Agent can retrieve and cite; Memory
is a curated fact about a user or Agent. Uploading a document does not put it
in chat context and does not grant every Agent access.

MyClaw is a cloud Agent platform. Knowledge ingestion is explicit through the
UI or API; it never scans a user's local directories, starts a directory
watcher, or performs scheduled directory synchronization.

## Ownership and access

Every `KnowledgeBase` belongs to one `tenant_id + owner_account_id`. The owner
must explicitly grant selected Agents and include the `knowledge_search` Tool
in each AgentVersion that may retrieve it. Both checks are required. A Group
receives no ambient access: each participating Agent uses its own grant under
the originating account authority.

User APIs never accept a caller-supplied Tenant. Authentication supplies the
Tenant/account authority, and guessed knowledge, document, source or Agent IDs
from another boundary fail as missing resources. Qdrant queries repeat the
Tenant and exact immutable revision filters; MySQL rechecks ownership and the
current revision before a result can be returned or snapshotted.

## Model identity and credentials

Chat and Embedding models share the global model registry but use distinct
logical `Model` and immutable `ModelVersion` identities. An Embedding version
declares:

- `model_kind: embedding`;
- a frozen `embedding_dimensions` value;
- an optional bounded `embedding_max_batch_size`;
- the OpenAI-compatible endpoint, Provider model name and write-only API Key.

It must pass `embedding-openai-compatible.v1` Conformance before activation.
An Embedding model cannot be selected as an Agent chat model. The same
supplier credential may be submitted for both model versions; MyClaw stores
each credential through its encrypted platform Secret store and never returns
the plaintext.

For Alibaba Cloud Model Studio/Bailian, the supported profile is:

```text
provider: openai_compatible
endpoint: https://dashscope.aliyuncs.com/compatible-mode/v1
model_name: text-embedding-v4
dimensions: 1024
```

## Authoritative data and vector projection

MySQL remains authoritative for ownership, source manifests, immutable index
revisions, chunk text, hashes and citations. Source bytes use the configured
object adapter; the single-node profile stores checksum-addressed files under
`KNOWLEDGE_ROOT`. Qdrant stores only a rebuildable vector projection plus
bounded identifiers used for Tenant/revision filtering. It is not the source
of truth for document text or authorization.

The data model separates:

- `KnowledgeDocument`: stable logical document and current source pointer;
- `KnowledgeSource`: immutable uploaded body, checksum and parse result;
- `KnowledgeLexiconRevision`: immutable canonical terms, aliases and stable
  term identities owned by one KnowledgeBase;
- `KnowledgeIndexRevision`: source manifest, frozen ModelVersion/profile and
  Dense/BM25/Lexicon projection state;
- `KnowledgeChunk`: revision-bound excerpt, hashes, matched term identities
  and Embedding checksum;
- `KnowledgeRetrievalSnapshot`: exact revision/chunk references used by a
  Runner, plus route/fusion/degradation evidence.

Existing feature-hash revisions stay readable. Selecting an Embedding model
for an existing base creates a new semantic revision without rewriting the
historical revisions or Runner evidence.

## Durable asynchronous indexing

Upload parsing validates the source and commits a `knowledge.index` Outbox
command. A dedicated `knowledge-worker` claims commands with a lease and
fencing token, parses/chunks outside a database transaction, calls the frozen
Embedding ModelVersion in bounded batches, computes pinned BM25 sparse weights
and lexicon term bindings, and idempotently upserts deterministic point IDs
with named `dense`, `bm25` and `lexicon` vectors into Qdrant. Only after every
point and every route is acknowledged
does a short transaction write chunk evidence, mark the revision `ready`, and
atomically move the KnowledgeBase current pointer.

The previous ready revision remains current while a replacement is `queued`
or `running`. Retryable Provider/Qdrant failures return the command to bounded
backoff; terminal failures retain a stable error code/detail and do not expose
a partial revision. A crashed worker's lease expires and another worker may
replay the same deterministic upserts safely.

## Retrieval and citations

Hybrid retrieval runs three independent candidate routes:

- `dense`: real query Embedding against the frozen dense ModelVersion;
- `bm25`: local deterministic Unicode/CJK tokenization, revision-frozen IDF
  and exact Qdrant sparse-vector scoring;
- `lexicon`: activated only when the query matches a canonical term or alias,
  then retrieves chunks bound to the same stable term identity.

MyClaw fuses route ranks with deterministic reciprocal-rank fusion (`k=60`).
It does not add raw cosine and BM25 scores, whose scales are not comparable.
Each route repeats the Tenant and exact revision filter. Query Embedding and
Qdrant calls happen with no database transaction open. A second short transaction
rechecks the owner, Agent grant and unchanged current revision, loads only the
candidate chunks, verifies source-body availability, and persists the Runner
snapshot.

Each result contains the KnowledgeBase, document/source/chunk IDs, exact index
revision, filename, page or Markdown section, source and chunk checksums,
RRF score, each participating route's rank/raw score, matched canonical
term/alias evidence and a bounded excerpt. The snapshot stores the query hash,
references and retrieval evidence, not a duplicate of the source excerpt.

If the Embedding Provider is temporarily unavailable, retrieval records an
explicit `dense` degradation and continues with BM25 plus any matched lexicon
route. No lexicon match is a normal skipped route, not an error. A Qdrant
failure affects all three projections and therefore remains fail-closed.

## Operator use

In **Admin → Models**, create a separate Embedding logical model, append a
version, run Conformance and activate it. In **Knowledge**, choose that model
when creating a base or apply it to an existing base. The console exposes
current/pending revision state, Qdrant point count, model identity, immutable
domain lexicon editing, failures, checksums, direct route evidence and exact
citations.

MySQL, source-object storage and the Qdrant volume must be protected by the
deployment's backup policy. Qdrant is reconstructible from the authoritative
revision/chunk/source data, but a restore procedure must not advertise a
semantic revision as available until its projection is present. The
single-node Compose profile therefore gives Qdrant a dedicated persistent
volume; clustered production deployments should use Qdrant replication and
snapshots.

OCR, cross-account sharing, automatic ingestion pipelines and production
multi-region vector replication remain outside this release.
