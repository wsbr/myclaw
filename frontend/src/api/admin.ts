import client from './client'

export interface PlatformAccount {
  id: string
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
}
