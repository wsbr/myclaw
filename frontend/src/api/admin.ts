import client from './client'

export const adminApi = {
  listUsers: () => client.get('/admin/users').then(r => r.data),
  createUser: (data: { email: string; username: string; password: string; role?: string }) =>
    client.post('/admin/users', data).then(r => r.data),
  updateUser: (id: string, data: { username?: string; role?: string; is_active?: boolean; password?: string }) =>
    client.put(`/admin/users/${id}`, data).then(r => r.data),
  deleteUser: (id: string) => client.delete(`/admin/users/${id}`),
  getStats: () => client.get('/admin/stats').then(r => r.data),
}
