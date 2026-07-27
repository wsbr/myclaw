import client from './client'

export interface NotificationItem {
  id: string
  agent_id: string | null
  session_id: string | null
  task_id: string | null
  channel: string
  title: string | null
  content: string
  is_read: boolean
  created_at: string
}

export const notificationsApi = {
  list: (unreadOnly = false) =>
    client.get<NotificationItem[]>('/notifications', { params: { unread_only: unreadOnly } }).then((r) => r.data),
  unreadCount: () =>
    client.get<{ count: number }>('/notifications/unread-count').then((r) => r.data.count),
  markRead: (id: string) => client.post(`/notifications/${id}/read`),
  markAllRead: () => client.post('/notifications/read-all'),
}
