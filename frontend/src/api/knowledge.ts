import client from './client'

export interface KnowledgeIndexRevision {
  id: string
  revision_no: number
  status: 'ready' | 'failed'
  source_manifest_hash: string
  document_count: number
  chunk_count: number
  content_hash: string
  built_at: string
}

export interface KnowledgeBase {
  id: string
  tenant_id: string
  owner_account_id: string
  name: string
  description: string
  sharing_policy: 'owner_only' | 'selected_agents'
  status: 'active' | 'deleted'
  agent_ids: string[]
  current_index_revision: KnowledgeIndexRevision | null
  document_count: number
  chunking_profile: Record<string, unknown>
  embedding_profile: Record<string, unknown>
  version: number
  created_at: string
  updated_at: string
}

export interface KnowledgeSource {
  id: string
  document_id: string
  checksum: string
  filename: string
  media_type: string
  size_bytes: number
  status: 'indexed' | 'superseded' | 'deleted' | 'failed'
  parse_metadata: Record<string, unknown>
  error_code: string | null
  error_detail: string | null
  created_at: string
}

export interface KnowledgeDocument {
  id: string
  knowledge_base_id: string
  title: string
  logical_key: string
  status: 'active' | 'deleted'
  current_source: KnowledgeSource | null
  version: number
  created_at: string
  updated_at: string
}

export interface KnowledgeSearchResult {
  knowledge_base_id: string
  knowledge_base_name: string
  index_revision_id: string
  document_id: string
  source_id: string
  chunk_id: string
  filename: string
  page_number: number | null
  section: string | null
  source_checksum: string
  chunk_checksum: string
  score: number
  excerpt: string
  citation: Record<string, unknown>
}

export interface KnowledgeStorageReconciliation {
  knowledge_base_id: string
  checked_source_count: number
  missing_sources: Array<{ source_id: string; storage_key: string; code: string; current: boolean }>
  corrupt_sources: Array<{ source_id: string; storage_key: string; code: string; current: boolean }>
  orphaned_bodies: string[]
  changed_source_ids: string[]
  restored_source_ids: string[]
  repair_applied: boolean
  index_revision: KnowledgeIndexRevision | null
  healthy: boolean
}

export const knowledgeApi = {
  list: () => client.get<{ items: KnowledgeBase[] }>('/knowledge-bases').then(r => r.data.items),
  create: (data: { name: string; description: string; agent_ids: string[] }) =>
    client.post<KnowledgeBase>('/knowledge-bases', data).then(r => r.data),
  updateAccess: (id: string, agentIds: string[]) =>
    client.patch<KnowledgeBase>(`/knowledge-bases/${id}/access`, { agent_ids: agentIds }).then(r => r.data),
  listDocuments: (id: string) =>
    client.get<{ items: KnowledgeDocument[] }>(`/knowledge-bases/${id}/documents`).then(r => r.data.items),
  upload: (id: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return client.post(`/knowledge-bases/${id}/documents`, form).then(r => r.data)
  },
  search: (id: string, query: string, limit = 8) =>
    client.post<{ query: string; count: number; results: KnowledgeSearchResult[]; revision_manifest: unknown[] }>(
      `/knowledge-bases/${id}/search`, { query, limit },
    ).then(r => r.data),
  reindex: (id: string) =>
    client.post<KnowledgeIndexRevision>(`/knowledge-bases/${id}/reindex`).then(r => r.data),
  reconcileStorage: (id: string, repair = true) =>
    client.post<KnowledgeStorageReconciliation>(
      `/knowledge-bases/${id}/reconcile-storage`, { repair },
    ).then(r => r.data),
  deleteDocument: (id: string, documentId: string) =>
    client.delete(`/knowledge-bases/${id}/documents/${documentId}`).then(r => r.data),
  deleteBase: (id: string) => client.delete(`/knowledge-bases/${id}`).then(r => r.data),
}
