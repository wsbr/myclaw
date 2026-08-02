import client from './client'

export interface MemorySource {
  message_id: string
  session_id: string
  source_role: string
  excerpt: string
}

export interface MemoryFact {
  id: string
  head_id: string
  scope: 'user' | 'user_agent'
  agent_id: string | null
  fact_key: string
  content: string
  status: 'active' | 'superseded' | 'deleted'
  version: number
  head_version: number
  source_kind: 'model' | 'user_edit' | 'import'
  confidence: number | null
  sources: MemorySource[]
  created_at: string
  updated_at: string
}

export const memoriesApi = {
  list: (status: MemoryFact['status'] = 'active') => client.get<{ items: MemoryFact[] }>(
    '/memories', { params: { status, limit: 100 } },
  ).then(response => response.data.items),
  versions: (memory: MemoryFact) => client.get<{ items: MemoryFact[] }>(
    `/memories/${memory.id}/versions`,
  ).then(response => response.data.items),
  edit: (memory: MemoryFact, content: string) => client.patch<MemoryFact>(
    `/memories/${memory.id}`, { content }, {
      headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${memory.head_version}"` },
    },
  ).then(response => response.data),
  delete: (memory: MemoryFact) => client.delete(
    `/memories/${memory.id}`, {
      headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${memory.head_version}"` },
    },
  ),
}
