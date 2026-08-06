import client from './client'

export interface AgentSummary {
  id: string
  owner_account_id: string
  visibility: 'private' | 'enterprise'
  status: 'draft' | 'published' | 'inactive'
  version: number
  draft_version_id: string
  current_published_version_id: string | null
  name: string
  description: string
  group_enabled: boolean
  latest_published_version_no: number | null
}

export interface AgentDraft {
  id: string
  agent_id: string
  draft_revision: number
  derived_from_version_id: string | null
  name: string
  description: string
  instructions: string
  model_id: string | null
  sandbox_profile_id: string | null
  context_policy_id: string | null
  history_policy_id: string | null
  memory_write_policy_id: string | null
  memory_recall_policy_id: string | null
  group_enabled: boolean
  tool_ids: string[]
  skill_ids: string[]
  bound_agent_ids: string[]
}

export interface AgentDetail { agent: AgentSummary; draft: AgentDraft }
export interface AgentOption { id: string; key?: string; name: string; description?: string }
export interface AgentPublishedVersion {
  id: string
  version_no: number
  publication_status: 'published' | 'deprecated'
  content_hash: string
  published_at: string | null
  model_version_id: string
  context_policy_version_id: string
  history_policy_version_id: string
  memory_write_policy_version_id: string
  memory_recall_policy_version_id: string
  sandbox_profile_version_id: string
  tools: Array<{ tool_id: string; registration_hash: string }>
  skills: Array<{ skill_id: string; skill_version_id: string; checksum: string }>
  agent_card_content_hash: string | null
}
export interface CapabilityCheck {
  publishable: boolean
  issues: string[]
  resolved_dependencies: Record<string, unknown> | null
}
export interface AgentApiKey {
  id: string
  agent_id: string
  name: string
  caller_label: string
  prefix: string
  status: string
  expires_at: string | null
  last_used_at: string | null
  rotated_from_id: string | null
  version: number
  created_at: string
  secret?: string | null
}

export interface AgentCard {
  agent_id: string
  agent_version_id: string
  card_content_hash: string
  name: string
  description: string
  identity_kind: string
  domain_tags: string[]
  supported_outcomes: string[]
  accepted_request_summary: string
  capabilities_summary: Record<string, unknown>
  collaboration_summary: Record<string, unknown>
  declaration: Record<string, unknown>
  attestation: Record<string, unknown>
  availability: 'available' | 'unavailable'
  group_enabled: boolean
  published_at: string
}

export const agentsApi = {
  searchNetwork: (params?: {
    query?: string
    domain_tag?: string[]
    capability?: string[]
    governance_label?: string[]
    group_enabled?: boolean
    limit?: number
  }) => client.get<{ items: AgentCard[]; next_cursor: string | null }>(
    '/agent-network/cards', { params },
  ).then(r => r.data),
  resolveNetworkAgent: (agentId: string) => client.get<AgentCard>(
    `/agent-network/agents/${agentId}`,
  ).then(r => r.data),
  list: () => client.get<{ items: AgentSummary[] }>('/agents').then(r => r.data.items),
  getSummary: (id: string) => client.get<AgentSummary>(`/agents/${id}/summary`).then(r => r.data),
  get: (id: string) => client.get<AgentDetail>(`/agents/${id}`).then(r => r.data),
  create: (draft: Partial<AgentDraft>, visibility = 'private') =>
    client.post<AgentDetail>('/agents', { visibility, draft }).then(r => r.data),
  patchDraft: (id: string, revision: number, data: Partial<AgentDraft>) =>
    client.patch<AgentDraft>(`/agents/${id}/draft`, data, { headers: { 'If-Match': `W/"${revision}"` } }).then(r => r.data),
  publish: (id: string, revision: number) =>
    client.post(`/agents/${id}/publish`, { expected_draft_revision: revision }, {
      headers: { 'Idempotency-Key': crypto.randomUUID() },
    }).then(r => r.data),
  testDraft: (id: string, revision: number, content: string) =>
    client.post(`/agents/${id}/draft/test-runs`, {
      expected_draft_revision: revision, content,
    }, { headers: { 'Idempotency-Key': crypto.randomUUID() } }).then(r => r.data),
  setState: (id: string, target: 'deactivate' | 'reactivate', reason: string) =>
    client.post<AgentSummary>(`/agents/${id}/${target}`, { reason }, {
      headers: { 'Idempotency-Key': crypto.randomUUID() },
    }).then(r => r.data),
  checkCapabilities: (id: string) =>
    client.get<CapabilityCheck>(`/agents/${id}/draft/capability-check`).then(r => r.data),
  listVersions: (id: string) =>
    client.get<{ items: AgentPublishedVersion[] }>(`/agents/${id}/versions`).then(r => r.data.items),
  listApiKeys: (id: string) => client.get<{ items: AgentApiKey[] }>(`/agents/${id}/api-keys`).then(r => r.data.items),
  createApiKey: (id: string, values: { name: string; caller_label: string; expires_at?: string | null }) =>
    client.post<AgentApiKey>(`/agents/${id}/api-keys`, values, {
      headers: { 'Idempotency-Key': crypto.randomUUID() },
    }).then(r => r.data),
  rotateApiKey: (id: string, key: AgentApiKey) =>
    client.post<AgentApiKey>(`/agents/${id}/api-keys/${key.id}/rotate`, {
      name: key.name, caller_label: key.caller_label, expires_at: key.expires_at,
    }, { headers: { 'Idempotency-Key': crypto.randomUUID() } }).then(r => r.data),
  revokeApiKey: (id: string, keyId: string) =>
    client.delete(`/agents/${id}/api-keys/${keyId}`, {
      headers: { 'Idempotency-Key': crypto.randomUUID() },
    }),
  options: (resource: string, params?: object) =>
    client.get<AgentOption[]>(`/agent-options/${resource}`, { params }).then(r => r.data),
}
