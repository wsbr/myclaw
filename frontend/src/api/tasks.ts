import client from './client'

export const tasksApi = {
  list: (params?: { agent_id?: string; status?: string }) =>
    client.get('/tasks', { params }).then((r) => r.data),
  get: (id: string) => client.get(`/tasks/${id}`).then((r) => r.data),
  create: (data: object) => client.post('/tasks', data).then((r) => r.data),
  cancel: (id: string) => client.delete(`/tasks/${id}`),
}
