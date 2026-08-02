import client from './client'

export const authApi = {
  login: (email: string, password: string) =>
    client.post('/auth/login', { email, password }).then((r) => r.data),
  enterpriseCallback: (assertion: string) =>
    client.get('/auth/enterprise/callback', { params: { assertion } }).then((r) => r.data),
  me: () => client.get('/auth/me').then((r) => r.data),
  logout: () => client.post('/auth/logout'),
}
