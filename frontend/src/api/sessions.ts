import client, { API_BASE } from './client'

export interface UserSession {
  id: string
  user_id: string
  entry_agent_id: string
  status: 'active' | 'closed' | 'archived'
  title: string | null
  origin_type: 'web' | 'api' | 'channel' | 'schedule'
  origin_schedule_id: string | null
  permission_mode: 'direct' | 'readonly' | 'plan_readonly'
  permission_revision: number
  interaction_mode: 'chat' | 'plan'
  interaction_revision: number
  version: number
  created_at: string
  updated_at: string
}

export interface Message {
  id: string
  session_id: string
  role: 'user' | 'assistant' | 'system' | 'tool_notice'
  sender_type: 'user' | 'agent' | 'system'
  sender_user_id: string | null
  sender_display_name: string | null
  is_external_human: boolean
  content: string
  content_format: string
  status: string
  run_id: string | null
  tool_activity_count: number
  created_at: string
}

export interface Run {
  id: string
  session_id: string
  mode: 'chat' | 'plan' | 'group'
  status: 'active' | 'waiting_user' | 'waiting_approval' | 'waiting_tool' | 'waiting_agent' | 'completed' | 'cancelled' | 'failed'
  wait_reason: string | null
  root_work_item_id: string | null
  permission_mode: 'direct' | 'readonly' | 'plan_readonly'
  version: number
  terminal_reason: string | null
  deadline_at: string | null
  cancellation_requested_at: string | null
  completed_at: string | null
}

export interface ExecutionEvent {
  event_id: string
  session_id: string
  run_id: string | null
  sequence: number
  event_type: string
  entity_type?: string
  entity_id?: string
  entity_version?: number | null
  summary: string
  payload: Record<string, unknown>
  created_at: string
}

export interface RunnerTraceView {
  status: 'available' | 'not_available'
  runner_id: string
  trace: null | { trace_id: string; status: string; sampled: boolean; error_code: string | null; started_at: string; ended_at: string | null }
  spans: Array<{
    span_id: string; parent_span_id: string | null; name: string; span_kind: string; status: 'ok' | 'error'
    started_at: string; ended_at: string; duration_ms: number; attributes: Record<string, unknown>
    input_tokens: number | null; output_tokens: number | null; error_code: string | null
  }>
  links: Array<{ span_id: string | null; linked_trace_id: string; linked_span_id: string | null; link_type: string; entity_type: string | null; entity_id: string | null }>
  ledger_refs: {
    run_id: string; work_item_id: string; runner_id: string
    agent_turn_ids: string[]; provider_attempt_ids: string[]
    tool_invocation_ids: string[]; tool_attempt_ids: string[]
    verification_result_ids: string[]
  }
}

export interface VerificationResult {
  id: string
  run_id: string
  rule_key: string
  rule_version: string
  verification_epoch: number
  status: 'passed' | 'failed' | 'insufficient_evidence'
  observed_facts: Array<Record<string, unknown>>
  missing_facts: Array<Record<string, unknown>>
  source_refs: Array<{
    evidence_type: 'tool_attempt' | 'artifact' | 'workspace_checksum' | 'business_fact'
    ref_id: string | null
    external_fact_ref: string | null
    workspace_generation: number | null
    checksum: string | null
  }>
  created_at: string
}

export interface Runner {
  id: string
  work_item_id: string
  run_id: string
  agent_id: string
  agent_version_id: string
  execution_epoch: number
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'lost'
  result_message_id: string | null
  error_code: string | null
  version: number
  created_at: string
  updated_at: string
}

export interface LiveDeltaEvent {
  event_type: 'model.delta'
  session_id: string
  run_id: string
  runner_id: string
  agent_turn_id: string
  stream_id: string
  sequence: number
  delta: string
}

export interface LiveStreamTerminalEvent {
  event_type: 'model.stream_reset' | 'model.stream_committed'
  session_id: string
  run_id: string
  runner_id: string
  agent_turn_id: string
  stream_id: string
  sequence: number
  reason_code?: string
  message_id?: string
}

export interface LiveToolProgressEvent {
  event_type: 'tool.progress'
  session_id: string
  run_id: string
  runner_id: string
  call_id: string
  tool_key: string
  status: 'queued' | 'running' | 'succeeded' | 'failed'
  tool_invocation_id?: string
}

export type LiveSessionEvent = LiveDeltaEvent | LiveStreamTerminalEvent | LiveToolProgressEvent

export interface ToolConfirmation {
  type: 'tool_confirmation'
  id: string
  tool_invocation_id: string
  session_id: string
  run_id: string
  tool_key: string
  arguments: Record<string, unknown>
  arguments_hash: string
  status: 'pending' | 'approved' | 'rejected' | 'cancelled'
  policy_version: number
  version: number
  created_at: string
  decided_at: string | null
}

export interface ToolInvocationView {
  id: string
  tool_key: string
  tool_definition_id: string
  tool_version_id: string | null
  tool_version_no: number | null
  tool_version_content_hash: string | null
  provider: string | null
  connection_revision_id: string | null
  connection_revision_no: number | null
  endpoint: string | null
  secret_fingerprint: string | null
  status: string
  arguments: Record<string, unknown>
  arguments_hash: string
  attempt_count: number
  result: {
    summary?: string; content_blocks?: Array<Record<string, unknown>>
    evidence?: Array<Record<string, unknown>>; artifact_ids?: string[]
    timing?: Record<string, unknown>; error?: Record<string, unknown>
  }
  attempts: Array<{
    id: string; attempt_no: number; status: string
    request: Record<string, unknown>; response: Record<string, unknown> | null
    retryable: boolean; error_code: string | null
    started_at: string | null; ended_at: string | null; latency_ms: number | null
  }>
  created_at: string
  updated_at: string
}

export interface ApprovalAction {
  type: 'tool' | 'session_grant' | 'plan_implementation'
  id: string
  session_id: string
  run_id: string
  work_item_id: string
  tool_invocation_id: string | null
  tool_key: string | null
  arguments: Record<string, unknown> | null
  subject_hash: string
  status: 'pending' | 'approved' | 'rejected' | 'cancelled'
  presentation: {
    title: string; summary: string
    working_plan_id?: string; plan_revision?: number
    permission_revision?: number; target_permission_mode?: string
  }
  version: number
  created_at: string
  decided_at: string | null
}

export interface WorkingPlan {
  id: string
  session_id: string
  run_id: string
  owner_agent_id: string
  summary: string
  revision: number
  status: 'active' | 'superseded' | 'closed'
  version: number
  items: Array<{ text: string; status: 'pending' | 'in_progress' | 'completed' }>
  explanation: string | null
  content_hash: string | null
  created_at: string
  updated_at: string
}

export interface AgentInvocation {
  id: string
  run_id: string
  caller_work_item_id: string
  caller_agent_id: string
  target_work_item_id: string | null
  target_agent_id: string
  target_agent_version_id: string | null
  return_route: 'caller' | 'direct' | 'group'
  request_text: string
  operation_key: string
  request_hash: string
  status: 'created' | 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'timed_out' | 'rejected'
  result_summary: string | null
  error_code: string | null
  result_message_id: string | null
  created_at: string
  completed_at: string | null
  version: number
}

export interface ClarificationAction {
  id: string
  type: 'clarification'
  status: 'pending' | 'answered' | 'cancelled'
  session_id: string
  run_id: string
  work_item_id: string
  asked_by_agent_id: string
  asked_by_agent_name?: string | null
  requester_account_id?: string | null
  requester_display_name?: string | null
  target_account_id?: string | null
  target_display_name?: string | null
  context_summary?: string | null
  source_session_owned?: boolean
  group_session_id: string | null
  group_membership_id: string | null
  question: string
  answer_text?: string | null
  answered_by_user_id?: string | null
  version: number
  created_at: string
}

export interface ReporterSelectionAction {
  id: string
  type: 'reporter_selection'
  status: 'pending'
  session_id: string
  run_id: string
  version: number
  presentation: {
    title: string
    summary: string
    group_run_id: string
    reporter_agent_id: string | null
  }
}

export interface GroupRun {
  id: string
  group_session_id: string
  reporter_agent_id: string | null
  summary_epoch: number
  delivered_summary_epoch: number
  goal: string
  display_phase?: string
  run: Run
  members: GroupRunMember[]
  shared_context: Record<string, unknown>
  budget_snapshot: Record<string, unknown>
  policy_snapshot: Record<string, unknown>
  version: number
}

export interface GroupMessage {
  id: string
  sender_type: 'user' | 'agent' | 'system'
  sender_agent_id: string | null
  recipient_agent_id: string | null
  visibility: 'direct' | 'group' | 'user'
  role: string
  content: string
  group_run_id?: string | null
  sequence_no?: number | null
  created_at: string
}

export interface GroupSession {
  id: string
  session_id: string
  status: 'open' | 'closed' | 'archived'
  initiator_agent_id: string
  membership_policy: 'fixed_for_run' | 'dynamic_future'
  formation_mode: 'manual_selected' | 'emergent_model_selected'
  version: number
}

export interface GroupMembership {
  id: string
  group_session_id: string
  agent_id: string
  status: 'invited' | 'joined' | 'declined' | 'cancelled' | 'left' | 'removed'
  invited_by_kind: 'user' | 'agent' | 'system'
  reason: string
  context_scope: Record<string, unknown>
  authorization_source: string
  joined_at: string | null
  ended_at: string | null
  version: number
}

export interface GroupRunMember {
  agent_id: string
  membership_id: string
  agent_version_id: string | null
  participation_status: 'available' | 'unavailable'
  unavailable_reason: string | null
  membership_policy: 'fixed_for_run'
  context_scope: Record<string, unknown>
  capability_boundary: Record<string, unknown>
  budget_snapshot: Record<string, unknown>
}

export interface GroupDiagnostic {
  id: string
  session_id: string
  run_id: string
  group_session_id: string
  group_run_id: string
  diagnostic_code: string
  category: 'delegation_guard' | 'progress_guard' | 'quiescence' | 'delivery'
  severity: 'info' | 'warning' | 'error'
  disposition: 'observed' | 'warned' | 'blocked' | 'recovered'
  source_entity_type: string
  source_entity_id: string
  source_event_id: string | null
  caller_agent_id: string | null
  target_agent_id: string | null
  causal_fingerprint: string
  causal_chain: string[]
  limit_snapshot: Record<string, unknown>
  safe_details: Record<string, unknown>
  summary: string
  created_at: string
}

export interface InteractionGraph {
  nodes: GroupRunMember[]
  edges: Array<{
    source_agent_id: string
    target_agent_id: string | null
    target_node_id: string
    cause_type: 'message' | 'group_message' | 'agent_invocation'
    cause_id: string
    status: string
    created_at: string
    result_message_id: string | null
    completed_at: string | null
  }>
}

export interface SharedTask {
  id: string
  group_session_id: string
  group_run_id: string | null
  title: string
  description: string
  status: 'pending' | 'in_progress' | 'completed' | 'blocked' | 'cancelled'
  assignee_agent_id: string | null
  depends_on_task_ids: string[]
  result_refs: Array<Record<string, unknown>>
  version: number
}

export interface Artifact {
  id: string
  session_id: string
  name: string
  mime_type: string
  size_bytes: number
  checksum: string
  created_by_agent_id: string
  work_item_id: string | null
  parent_artifact_id: string | null
  status: 'published' | 'unavailable' | 'deleted'
  version_no: number
  version: number
  created_at: string
}

export type RequiredAction = ApprovalAction | ToolConfirmation | ClarificationAction | ReporterSelectionAction

const idempotency = () => ({ headers: { 'Idempotency-Key': crypto.randomUUID() } })

export const sessionsApi = {
  list: () => client.get<{ items: UserSession[] }>('/sessions').then(r => r.data.items),
  create: (entryAgentId: string) => client.post<UserSession>(
    '/sessions', { entry_agent_id: entryAgentId }, idempotency(),
  ).then(r => r.data),
  get: (id: string) => client.get<UserSession>(`/sessions/${id}`).then(r => r.data),
  close: (id: string) => client.delete(`/sessions/${id}`, idempotency()),
  getMessages: (id: string) => client.get<{ items: Message[] }>(`/sessions/${id}/messages`).then(r => r.data.items),
  sendMessage: (id: string, content: string, mentionAccountIds: string[] = []) => client.post<{ message: Message; run: Run; links: Record<string, string> }>(
    `/sessions/${id}/messages`, {
      content,
      mentions: mentionAccountIds.map(account_id => ({ account_id })),
    }, idempotency(),
  ).then(r => r.data),
  getSessionRuns: (id: string, mode?: Run['mode']) => client.get<{ items: Run[] }>(
    `/sessions/${id}/runs`, { params: { mode } },
  ).then(r => r.data.items),
  enterPlanMode: (session: UserSession, goal: string) => client.post<{
    message: Message; run: Run; session: UserSession; links: Record<string, string>
  }>(`/sessions/${session.id}/plan-mode`, { goal }, {
    headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${session.version}"` },
  }).then(r => r.data),
  exitPlanMode: (session: UserSession) => client.post<UserSession>(
    `/sessions/${session.id}/plan-mode/exit`, {}, {
      headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${session.version}"` },
    },
  ).then(r => r.data),
  continuePlanning: (plan: WorkingPlan, instruction: string) => client.post<{
    message: Message; run: Run; session: UserSession
  }>(`/working-plans/${plan.id}/continue-planning`, {
    instruction, expected_revision: plan.revision,
  }, {
    headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${plan.version}"` },
  }).then(r => r.data),
  approvePlanImplementation: (plan: WorkingPlan, subjectHash: string) => client.post<ApprovalAction>(
    `/working-plans/${plan.id}/approve-implementation`, {
      expected_revision: plan.revision, subject_hash: subjectHash,
    }, {
      headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${plan.version}"` },
    },
  ).then(r => r.data),
  listGroups: (sessionId: string) => client.get<{ items: GroupSession[] }>(
    `/sessions/${sessionId}/groups`,
  ).then(r => r.data.items),
  createGroup: (
    sessionId: string,
    values: {
      member_agent_ids: string[]
      goal: string
      reporter_agent_id?: string
      shared_context_refs?: string[]
    },
  ) => client.post<GroupSession>(`/sessions/${sessionId}/groups`, values, idempotency()).then(r => r.data),
  getGroup: (groupId: string) => client.get<GroupSession>(`/groups/${groupId}`).then(r => r.data),
  getGroupMemberships: (groupId: string) => client.get<{ items: GroupMembership[] }>(
    `/groups/${groupId}/memberships`,
  ).then(r => r.data.items),
  getGroupRuns: (groupId: string) => client.get<{ items: GroupRun[] }>(`/groups/${groupId}/runs`).then(r => r.data.items),
  createGroupRun: (groupId: string, content: string, recipientAgentId?: string) => client.post<GroupRun>(
    `/groups/${groupId}/runs`, { content, recipient_agent_id: recipientAgentId, reference_run_ids: [] }, idempotency(),
  ).then(r => r.data),
  getGroupMessages: (groupId: string) => client.get<{ items: GroupMessage[] }>(
    `/groups/${groupId}/messages`,
  ).then(r => r.data.items),
  getGroupRunMessages: (groupRunId: string) => client.get<{ items: GroupMessage[] }>(
    `/group-runs/${groupRunId}/messages`,
  ).then(r => r.data.items),
  sendGroupRunMessage: (groupRunId: string, content: string, recipientAgentId?: string) => client.post(
    `/group-runs/${groupRunId}/messages`, { content, recipient_agent_id: recipientAgentId }, idempotency(),
  ).then(r => r.data),
  getInteractionGraph: (groupRunId: string) => client.get<InteractionGraph>(
    `/group-runs/${groupRunId}/interaction-graph`,
  ).then(r => r.data),
  getGroupDiagnostics: (groupRunId: string) => client.get<{ items: GroupDiagnostic[] }>(
    `/group-runs/${groupRunId}/diagnostics`,
  ).then(r => r.data.items),
  getSharedTasks: (groupId: string) => client.get<{ items: SharedTask[] }>(
    `/groups/${groupId}/shared-tasks`,
  ).then(r => r.data.items),
  replaceGroupReporter: (groupRun: GroupRun, reporterAgentId: string) => client.post<GroupRun>(
    `/group-runs/${groupRun.id}/reporter`, { reporter_agent_id: reporterAgentId }, {
      headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${groupRun.version}"` },
    },
  ).then(r => r.data),
  cancelGroupRun: (groupRun: GroupRun, reason: string) => client.post<GroupRun>(
    `/group-runs/${groupRun.id}/cancel`, { reason }, {
      headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${groupRun.run.version}"` },
    },
  ).then(r => r.data),
  closeGroup: (groupId: string, version: number, reason: string) => client.post(
    `/groups/${groupId}/close`, { reason }, {
      headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${version}"` },
    },
  ).then(r => r.data),
  getRun: (id: string) => client.get<Run>(`/runs/${id}`).then(r => r.data),
  getWorkingPlans: (id: string) => client.get<{ items: WorkingPlan[] }>(
    `/runs/${id}/working-plans`,
  ).then(r => r.data.items),
  getAgentInvocations: (id: string) => client.get<{ items: AgentInvocation[] }>(
    `/runs/${id}/agent-invocations`,
  ).then(r => r.data.items),
  getToolInvocations: (id: string) => client.get<{ items: ToolInvocationView[] }>(
    `/runs/${id}/tool-invocations`,
  ).then(r => r.data.items),
  getRunTimeline: (id: string, afterSequence = 0) => client.get<{
    items: ExecutionEvent[]; scanned_through_sequence: number; session_high_watermark: number; has_more: boolean
  }>(`/runs/${id}/timeline`, { params: { after_sequence: afterSequence } }).then(r => r.data),
  getRunRunners: (id: string) => client.get<{ items: Runner[] }>(`/runs/${id}/runners`).then(r => r.data.items),
  getRunnerTrace: (id: string) => client.get<RunnerTraceView>(`/runners/${id}/trace`).then(r => r.data),
  getVerificationResults: (id: string) => client.get<{ items: VerificationResult[] }>(
    `/runs/${id}/verification-results`,
  ).then(r => r.data.items),
  cancelRun: (run: Run, reason: string) => client.post<Run>(
    `/runs/${run.id}/cancel`, { reason }, {
      headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${run.version}"` },
    },
  ).then(r => r.data),
  getRequiredActions: () => client.get<{ items: RequiredAction[] }>('/required-actions').then(r => r.data.items),
  decideToolConfirmation: (action: ToolConfirmation, decision: 'approve' | 'reject', reason?: string) =>
    client.post<ToolConfirmation>(`/tool-confirmations/${action.id}/${decision}`, { arguments_hash: action.arguments_hash, reason }, {
      headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${action.version}"` },
    }).then(r => r.data),
  decideApproval: (action: ApprovalAction, decision: 'approve' | 'reject', reason?: string) =>
    client.post<ApprovalAction>(`/approvals/${action.id}/${decision}`, {
      subject_hash: action.subject_hash, reason,
    }, {
      headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${action.version}"` },
    }).then(r => r.data),
  answerClarification: (action: ClarificationAction, answer: string) => client.post<ClarificationAction>(
    `/clarifications/${action.id}/answer`, { answer }, {
      headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${action.version}"` },
    },
  ).then(r => r.data),
  getEvents: (id: string, afterSequence: number) => client.get<{
    items: ExecutionEvent[]; scanned_through_sequence: number; session_high_watermark: number; has_more: boolean
  }>(`/sessions/${id}/events`, { params: { after_sequence: afterSequence } }).then(r => r.data),
  getArtifacts: (id: string) => client.get<{ items: Artifact[] }>(
    `/sessions/${id}/artifacts`, { params: { limit: 100 } },
  ).then(r => r.data.items),
  downloadArtifact: async (artifact: Artifact) => {
    const response = await client.get<Blob>(`/artifacts/${artifact.id}/download`, { responseType: 'blob' })
    const url = URL.createObjectURL(response.data)
    const anchor = document.createElement('a')
    anchor.href = url; anchor.download = artifact.name; anchor.click()
    URL.revokeObjectURL(url)
  },
  deleteArtifact: (artifact: Artifact) => client.post<Artifact>(
    `/artifacts/${artifact.id}/delete`, {}, {
      headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${artifact.version}"` },
    },
  ).then(r => r.data),
}

export function subscribeSessionEvents(
  sessionId: string,
  afterSequence: number,
  onEvent: (event: ExecutionEvent | LiveSessionEvent | { event_type: 'cursor'; sequence: number }) => void,
  onConnection: (connected: boolean) => void,
  onCursorReset?: () => void,
): () => void {
  const controller = new AbortController()
  const seenEventIds = new Set<string>()
  const seenEventOrder: string[] = []
  const retryDelays = [1000, 2000, 5000, 10000, 30000]
  const cursorKey = `myclaw:session-event-cursor:${sessionId}`

  const remember = (event: ExecutionEvent) => {
    if (seenEventIds.has(event.event_id)) return false
    seenEventIds.add(event.event_id)
    seenEventOrder.push(event.event_id)
    if (seenEventOrder.length > 512) {
      const expired = seenEventOrder.shift()
      if (expired) seenEventIds.delete(expired)
    }
    return true
  }

  const wait = (milliseconds: number) => new Promise<void>(resolve => {
    const timer = window.setTimeout(resolve, milliseconds)
    controller.signal.addEventListener('abort', () => {
      window.clearTimeout(timer)
      resolve()
    }, { once: true })
  })

  ;(async () => {
    const saved = Number(localStorage.getItem(cursorKey) || '0')
    let cursor = Math.max(afterSequence, Number.isFinite(saved) ? saved : 0)
    let retry = 0
    while (!controller.signal.aborted) {
      try {
        // Repair reliable events from MySQL before every live connection. The
        // scanned cursor advances even when every event in a range is private.
        let hasMore = true
        while (hasMore && !controller.signal.aborted) {
          try {
            const page = await sessionsApi.getEvents(sessionId, cursor)
            for (const event of page.items) {
              cursor = Math.max(cursor, event.sequence)
              if (remember(event)) onEvent(event)
            }
            cursor = Math.max(cursor, page.scanned_through_sequence)
            localStorage.setItem(cursorKey, String(cursor))
            onEvent({ event_type: 'cursor', sequence: cursor })
            hasMore = page.has_more
          } catch (error: any) {
            if (error?.response?.status !== 410) throw error
            cursor = 0
            localStorage.removeItem(cursorKey)
            seenEventIds.clear()
            seenEventOrder.length = 0
            onCursorReset?.()
            hasMore = false
          }
        }

        const token = localStorage.getItem('access_token')
        const response = await fetch(`${API_BASE}/sessions/${sessionId}/events/stream?after_sequence=${cursor}`, {
          headers: {
            Authorization: `Bearer ${token}`,
            ...(cursor > 0 ? { 'Last-Event-ID': String(cursor) } : {}),
          },
          signal: controller.signal,
        })
        if (response.status === 410) {
          cursor = 0
          localStorage.removeItem(cursorKey)
          seenEventIds.clear()
          seenEventOrder.length = 0
          onCursorReset?.()
          continue
        }
        if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`)
        onConnection(true)
        retry = 0
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        while (!controller.signal.aborted) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const frames = buffer.split('\n\n')
          buffer = frames.pop() ?? ''
          for (const frame of frames) {
            let eventType = ''
            let data = ''
            let id = ''
            for (const line of frame.split('\n')) {
              if (line.startsWith('event: ')) eventType = line.slice(7)
              if (line.startsWith('data: ')) data += line.slice(6)
              if (line.startsWith('id: ')) id = line.slice(4)
            }
            if (!eventType) continue
            if (id) {
              cursor = Math.max(cursor, Number(id))
              localStorage.setItem(cursorKey, String(cursor))
            }
            if (eventType === 'cursor' && id) onEvent({ event_type: 'cursor', sequence: cursor })
            else if (data) {
              const event = JSON.parse(data) as ExecutionEvent | LiveSessionEvent
              if (event.event_type.startsWith('model.') || event.event_type === 'tool.progress') onEvent(event)
              else if ('event_id' in event && remember(event)) onEvent(event)
            }
          }
        }
        if (!controller.signal.aborted) {
          onConnection(false)
          await wait(retryDelays[Math.min(retry, retryDelays.length - 1)])
          retry += 1
        }
      } catch (error: any) {
        if (error?.name === 'AbortError') break
        onConnection(false)
        await wait(retryDelays[Math.min(retry, retryDelays.length - 1)])
        retry += 1
      }
    }
  })()
  return () => controller.abort()
}
