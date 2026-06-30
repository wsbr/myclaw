import client from './client'

export const agentsApi = {
  list: () => client.get('/agents').then(r => r.data),
  get: (id: string) => client.get(`/agents/${id}`).then(r => r.data),
  create: (data: object) => client.post('/agents', data).then(r => r.data),
  update: (id: string, data: object) => client.put(`/agents/${id}`, data).then(r => r.data),
  delete: (id: string) => client.delete(`/agents/${id}`),
  activate: (id: string) => client.post(`/agents/${id}/activate`).then(r => r.data),
  duplicate: (id: string) => client.post(`/agents/${id}/duplicate`).then(r => r.data),
  listVersions: (id: string) => client.get(`/agents/${id}/versions`).then(r => r.data),
  updateTools: (id: string, data: object) => client.put(`/agents/${id}/tools`, data),
  updateSkills: (id: string, data: object) => client.put(`/agents/${id}/skills`, data),
  updateMcpServers: (id: string, data: object) => client.put(`/agents/${id}/mcp-servers`, data),
  listApiTokens: (id: string) => client.get(`/agents/${id}/api-tokens`).then(r => r.data),
  createApiToken: (id: string, name: string) => client.post(`/agents/${id}/api-tokens`, { name }).then(r => r.data),
  deleteApiToken: (agentId: string, tokenId: string) => client.delete(`/agents/${agentId}/api-tokens/${tokenId}`),
}
