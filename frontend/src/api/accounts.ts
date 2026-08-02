import client from './client'

export interface MyAccount {
  id: string
  user_id: string
  email: string | null
  display_name: string
  status: string
  roles: string[]
  version: number
  auth_revision: number
  created_at: string
  updated_at: string
}

const commandHeaders = (version: number) => ({
  'If-Match': `W/"${version}"`,
  'Idempotency-Key': crypto.randomUUID(),
})

export const accountsApi = {
  me: () => client.get<MyAccount>('/accounts/me').then(response => response.data),
  update: (values: { display_name?: string; email?: string | null }, version: number) =>
    client.patch<MyAccount>('/accounts/me', values, { headers: commandHeaders(version) })
      .then(response => response.data),
  changeUserId: (enterprise_assertion: string, version: number) =>
    client.post<MyAccount>('/accounts/me/change-user-id', { enterprise_assertion }, {
      headers: commandHeaders(version),
    }).then(response => response.data),
}
