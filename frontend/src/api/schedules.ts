import client from './client'

export type ScheduleType = 'once' | 'cron' | 'interval'
export type ScheduleStatus = 'active' | 'paused' | 'completed' | 'disabled'

export interface AgentSchedule {
  id: string
  agent_id: string
  agent_name: string
  name: string
  prompt: string
  context_mode: 'new_session' | 'continue_session'
  source_session_id: string | null
  schedule_type: ScheduleType
  cron_expression: string | null
  interval_seconds: number | null
  run_at: string | null
  timezone: string
  next_fire_at: string | null
  next_fire_local: string | null
  end_at: string | null
  version_policy: 'latest_published' | 'pinned'
  pinned_agent_version_id: string | null
  overlap_policy: 'skip' | 'queue_one' | 'allow'
  misfire_policy: 'skip' | 'fire_once'
  misfire_grace_seconds: number
  status: ScheduleStatus
  total_fire_count: number
  last_scheduled_for: string | null
  last_fired_at: string | null
  last_error_code: string | null
  last_error_detail: string | null
  version: number
  created_at: string
  updated_at: string
}

export interface AgentScheduleFire {
  id: string
  schedule_id: string
  schedule_name: string
  agent_id: string
  agent_name: string
  trigger_kind: 'scheduled' | 'manual'
  context_mode: 'new_session' | 'continue_session'
  scheduled_for: string
  scheduled_for_local: string
  status: 'queued' | 'deferred' | 'dispatching' | 'running' | 'succeeded' | 'failed' | 'skipped' | 'cancelled'
  reason_code: string | null
  agent_version_id: string | null
  session_id: string | null
  run_id: string | null
  runner_id: string | null
  result_preview: string | null
  fired_at: string | null
  completed_at: string | null
  notification_read_at: string | null
  error_code: string | null
  error_detail: string | null
  created_at: string
  updated_at: string
}

export interface AgentScheduleInput {
  agent_id: string
  name: string
  prompt: string
  schedule_type: ScheduleType
  run_at: string | null
  cron_expression: string | null
  interval_seconds: number | null
  timezone: string
  end_at: string | null
  version_policy: 'latest_published' | 'pinned'
  pinned_agent_version_id: string | null
  overlap_policy: 'skip' | 'queue_one' | 'allow'
  misfire_policy: 'skip' | 'fire_once'
  misfire_grace_seconds: number
}

export const schedulesApi = {
  list: () => client.get<AgentSchedule[]>('/schedules').then(r => r.data),
  create: (input: AgentScheduleInput) => client.post<AgentSchedule>('/schedules', input, {
    headers: { 'Idempotency-Key': crypto.randomUUID() },
  }).then(r => r.data),
  update: (item: AgentSchedule, input: Partial<AgentScheduleInput>) =>
    client.patch<AgentSchedule>(`/schedules/${item.id}`, input, {
      headers: { 'If-Match': `W/"${item.version}"` },
    }).then(r => r.data),
  pause: (item: AgentSchedule) => client.post<AgentSchedule>(
    `/schedules/${item.id}/pause`, {},
    { headers: { 'If-Match': `W/"${item.version}"` } },
  ).then(r => r.data),
  resume: (item: AgentSchedule) => client.post<AgentSchedule>(
    `/schedules/${item.id}/resume`, {},
    { headers: { 'If-Match': `W/"${item.version}"` } },
  ).then(r => r.data),
  runNow: (item: AgentSchedule) => client.post<AgentScheduleFire>(
    `/schedules/${item.id}/run-now`, {},
    { headers: { 'Idempotency-Key': crypto.randomUUID() } },
  ).then(r => r.data),
  disable: (item: AgentSchedule) => client.delete(`/schedules/${item.id}`, {
    headers: { 'If-Match': `W/"${item.version}"` },
  }),
  fires: (id: string) => client.get<AgentScheduleFire[]>(`/schedules/${id}/fires`).then(r => r.data),
  unreadNotifications: () => client.get<AgentScheduleFire[]>('/schedules/notifications/unread').then(r => r.data),
  readNotification: (id: string) => client.post(`/schedules/notifications/${id}/read`),
  readAllNotifications: () => client.post('/schedules/notifications/read-all'),
}
