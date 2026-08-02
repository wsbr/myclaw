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
}
