import client from './client'

export interface PlatformAccount {
  id: string
  tenant_id: string
  tenant: { id: string; slug: string; name: string; status: string } | null
  user_id: string
  email: string | null
  display_name: string
  status: 'active' | 'disabled' | 'discarded'
  roles: Array<'user' | 'builder' | 'admin'>
  version: number
  auth_revision: number
  created_at: string
  updated_at: string
}

export interface RuntimeRunProjection {
  id: string
  session_id: string
  account_id: string | null
  mode: 'chat' | 'plan' | 'group'
  status: string
  wait_reason: string | null
  terminal_reason: string | null
  root_work_item_id: string | null
  permission_mode: string
  created_at: string
  updated_at: string
  completed_at: string | null
  deadline_at: string | null
  age_seconds: number
  counts: {
    work_items: Record<string, number>
    runners: Record<string, number>
    tool_invocations: Record<string, number>
    agent_invocations: Record<string, number>
    diagnostics: number
  }
  budget: {
    limits: Record<string, number>
    usage: Record<string, number>
    warning_dimensions: string[]
  }
  last_progress: null | {
    made_progress: boolean
    reason_code: string
    consecutive_no_progress: number
    created_at: string
  }
}

export interface RuntimeSummary {
  generated_at: string
  runs: Record<string, number>
  runners: Record<string, number>
  tool_invocations: Record<string, number>
  agent_invocations: Record<string, number>
  outbox: Array<{ topic: string; status: string; count: number; oldest_age_seconds: number | null }>
  observed_workers: Array<{
    worker_kind: string
    worker_id: string
    observed_active_slots: number
    last_heartbeat_at: string | null
    stale_leases: number
    health: 'active' | 'stale'
  }>
  outcome_unknown: { provider_attempts: number; tool_attempts: number }
  diagnostic_count: number
}

export interface RuntimeRunDetail extends RuntimeRunProjection {
  work_items: Array<Record<string, unknown>>
  runners: Array<Record<string, unknown>>
  provider_attempts: Array<Record<string, unknown>>
  tool_invocations: Array<Record<string, unknown>>
  agent_invocations: Array<Record<string, unknown>>
  diagnostics: Array<Record<string, unknown>>
  timeline: Array<Record<string, unknown>>
  outbox: Array<Record<string, unknown>>
}

export interface UsageLedgerEntry {
  id: string
  run_id: string
  group_run_id: string | null
  agent_version_id: string | null
  model_version_id: string | null
  source_type: string
  source_id: string
  metric: string
  unit: string
  quantity: string
  measurement_status: 'measured' | 'estimated' | 'unknown'
  price_snapshot_id: string | null
  currency: string | null
  cost_amount: string | null
  cost_status: 'estimated' | 'contracted' | 'unknown'
  adjusts_entry_id: string | null
  created_at: string
}

export interface UsagePriceSnapshot {
  id: string
  subject_type: string
  provider: string | null
  subject_key: string
  metric: string
  unit: string
  currency: string
  price_per_unit: string
  price_status: 'estimated' | 'contracted'
  effective_from: string
  effective_to: string | null
  source_label: string
  content_hash: string
  created_at: string
}

export interface UsageRollup {
  key: string
  entry_count: number
  quantities: Record<string, string>
  costs: Record<string, string>
  unknown_cost_entries: number
}

const commandHeaders = (version?: number) => ({
  headers: {
    'Idempotency-Key': crypto.randomUUID(),
    ...(version === undefined ? {} : { 'If-Match': `W/"${version}"` }),
  },
})

export const adminApi = {
  listAccounts: () => client.get<{ items: PlatformAccount[]; next_cursor: string | null }>('/admin/accounts').then(r => r.data),
  createAccount: (data: { user_id?: string; email: string; display_name: string; password: string; roles: string[] }) =>
    client.post<PlatformAccount>('/admin/accounts', data, commandHeaders()).then(r => r.data),
  updateRoles: (account: PlatformAccount, roles: string[]) =>
    client.patch<PlatformAccount>(`/admin/accounts/${account.id}/roles`, { roles }, commandHeaders(account.version)).then(r => r.data),
  updateStatus: (account: PlatformAccount, status: 'active' | 'disabled') =>
    client.patch<PlatformAccount>(`/admin/accounts/${account.id}/status`, { status }, commandHeaders(account.version)).then(r => r.data),
  getStats: () => client.get('/admin/stats').then(r => r.data),
  getRuntimeSummary: () => client.get<RuntimeSummary>('/admin/runtime/summary').then(r => r.data),
  listRuntimeRuns: (params: Record<string, string>) => client.get<{ items: RuntimeRunProjection[]; next_cursor: string | null }>(
    '/admin/runtime/runs', { params },
  ).then(r => r.data),
  getRuntimeRun: (runId: string) => client.get<RuntimeRunDetail>(`/admin/runtime/runs/${runId}`).then(r => r.data),
  listRuntimeOutbox: (params: Record<string, string>) => client.get<{ items: Array<Record<string, unknown>>; next_cursor: string | null }>(
    '/admin/runtime/outbox', { params },
  ).then(r => r.data),
  listUsageEntries: (params: Record<string, string> = {}) => client.get<{ items: UsageLedgerEntry[]; next_cursor: string | null }>(
    '/admin/usage/entries', { params },
  ).then(r => r.data),
  getUsageRollups: (params: Record<string, string> = {}) => client.get<{ group_by: string; items: UsageRollup[] }>(
    '/admin/usage/rollups', { params },
  ).then(r => r.data),
  listUsagePrices: (params: Record<string, string> = {}) => client.get<{ items: UsagePriceSnapshot[]; next_cursor: string | null }>(
    '/admin/usage/prices', { params },
  ).then(r => r.data),
  createUsagePrice: (data: Record<string, unknown>) => client.post<UsagePriceSnapshot>(
    '/admin/usage/prices', data, commandHeaders(),
  ).then(r => r.data),
}
