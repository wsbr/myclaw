import client, { API_BASE } from './client'

export interface UserSession {
  id: string
  user_id: string
  entry_agent_id: string
  status: 'active' | 'closed' | 'archived'
  title: string | null
  origin_type: 'web' | 'api' | 'channel'
  version: number
  created_at: string
  updated_at: string
}

export interface Message {
  id: string
  session_id: string
  role: 'user' | 'assistant' | 'system' | 'tool_notice'
  sender_type: 'user' | 'agent' | 'system'
  content: string
  content_format: string
  status: string
  run_id: string | null
  created_at: string
}

export interface Run {
  id: string
  session_id: string
  mode: 'chat' | 'plan' | 'group'
  execution_kind: string
  lifecycle: 'running' | 'waiting' | 'settling' | 'completed' | 'cancelled'
  wait_reason: string | null
  outcome: 'succeeded' | 'partially_succeeded' | 'failed' | null
  version: number
  result_summary: string | null
  error_code: string | null
  cancellation_requested_at: string | null
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
  delta: string
}

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

export interface PlanNode {
  id: string
  node_key: string
  title: string
  goal: string
  acceptance_criteria: string
  assigned_agent_id: string
  agent_version_id: string | null
  work_item_id: string | null
  sort_order: number
  definition_version: number
}

export interface PlanDependency {
  predecessor_node_key: string
  successor_node_key: string
  dependency_type: 'success' | 'settled'
}

export interface PlanExecution {
  id: string
  run_id: string
  session_id: string
  owner_agent_id: string
  owner_agent_version_id: string | null
  predecessor_plan_id: string | null
  goal: string
  deliverable: string
  definition_status: 'draft' | 'awaiting_confirmation' | 'frozen' | 'cancelled'
  graph_revision: number
  confirmed_at: string | null
  frozen_hash: string | null
  version: number
  declared_agents: { agent_id: string; agent_version_id: string }[]
  nodes: PlanNode[]
  dependencies: PlanDependency[]
  run: Run
}

export interface PlanWorkItem {
  id: string
  plan_node_id: string | null
  status: string
  owner_agent_id: string
  result_summary: string | null
  error_code: string | null
  version: number
}

export interface PlanConfirmationAction {
  id: string
  type: 'plan_confirmation'
  status: 'pending'
  session_id: string
  run_id: string
  version: number
  graph_revision: number
  presentation: { title: string; summary: string }
}

export interface ClarificationAction {
  id: string
  type: 'clarification'
  status: 'pending' | 'answered' | 'cancelled'
  session_id: string
  run_id: string
  work_item_id: string
  asked_by_agent_id: string
  group_session_id: string | null
  group_node_id: string | null
  question: string
  version: number
  created_at: string
}

export interface GroupConfirmationAction {
  id: string
  type: 'group_confirmation'
  status: 'pending'
  session_id: string
  run_id: string
  version: number
  revision_no: number
}

export interface GroupParticipant {
  id: string
  participant_key: string
  agent_id: string
  is_entry: boolean
  initial_responsibility: string
  sort_order: number
}

export interface GroupFormationRevision {
  id: string
  revision_no: number
  goal: string
  bootstrap_context: string
  content_hash: string
  participants: GroupParticipant[]
  initial_work_items: Array<{ id: string; work_item_key: string; participant_key: string; title: string; goal: string; acceptance_criteria: string; sort_order: number }>
  suggested_edges: Array<{ id: string; source_participant_key: string; target_participant_key: string; label: string; rationale: string; sort_order: number }>
}

export interface GroupFormationProposal {
  id: string
  parent_session_id: string
  group_session_id: string
  entry_agent_id: string
  status: 'generating' | 'awaiting_confirmation' | 'confirmed' | 'rejected' | 'cancelled' | 'failed'
  current_revision_no: number
  version: number
  group_session: { id: string; status: 'forming' | 'open' | 'closed' | 'archived'; version: number }
  generation_run: { id: string; lifecycle: Run['lifecycle']; outcome: Run['outcome']; version: number }
  current_revision: GroupFormationRevision | null
}

export interface GroupNode {
  id: string
  agent_id: string
  is_entry: boolean
  initial_responsibility: string
  availability: 'available' | 'unavailable'
  unavailable_reason: string | null
  current_agent_version_id: string | null
  version: number
}

export interface GroupSuggestedEdge {
  source_node_id: string
  target_node_id: string
  label: string
  rationale: string
}

export interface GroupCollaborationEdge {
  id: string
  source_node_id: string
  target_node_id: string
  first_invocation_id: string
  created_in_group_run_id: string
  created_at: string
}

export interface GroupRun {
  id: string
  group_session_id: string
  owner_node_id: string
  goal: string
  display_phase: string
  run: Run
  agent_versions: Array<{ group_node_id: string; participation_status: 'available' | 'unavailable'; agent_version_id: string | null; unavailable_reason: string | null }>
}

export interface GroupMessage {
  id: string
  sender_type: 'user' | 'agent' | 'system'
  sender_agent_id: string | null
  recipient_group_node_id: string | null
  role: string
  content: string
  created_at: string
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

export type RequiredAction = ToolConfirmation | PlanConfirmationAction | ClarificationAction | GroupConfirmationAction

const idempotency = () => ({ headers: { 'Idempotency-Key': crypto.randomUUID() } })

export const sessionsApi = {
  list: () => client.get<{ items: UserSession[] }>('/sessions').then(r => r.data.items),
  create: (entryAgentId: string) => client.post<UserSession>(
    '/sessions', { entry_agent_id: entryAgentId, origin_type: 'web' }, idempotency(),
  ).then(r => r.data),
  get: (id: string) => client.get<UserSession>(`/sessions/${id}`).then(r => r.data),
  close: (id: string) => client.delete(`/sessions/${id}`, idempotency()),
  getMessages: (id: string) => client.get<{ items: Message[] }>(`/sessions/${id}/messages`).then(r => r.data.items),
  sendMessage: (id: string, content: string) => client.post<{ message: Message; run: Run; links: Record<string, string> }>(
    `/sessions/${id}/messages`, { content, mode: 'chat' }, idempotency(),
  ).then(r => r.data),
  listPlans: (id: string) => client.get<{ items: PlanExecution[] }>(`/sessions/${id}/plans`).then(r => r.data.items),
  createPlan: (id: string, goal: string, predecessorPlanId?: string) => client.post<{
    plan: PlanExecution; generation_run: Run
  }>(`/sessions/${id}/plans`, { goal, predecessor_plan_id: predecessorPlanId }, idempotency()).then(r => r.data),
  updatePlan: (plan: PlanExecution, value: Pick<PlanExecution, 'goal' | 'deliverable' | 'nodes' | 'dependencies'>) =>
    client.patch<PlanExecution>(`/plans/${plan.id}`, {
      goal: value.goal, deliverable: value.deliverable,
      nodes: value.nodes.map(({ node_key, title, goal, acceptance_criteria, assigned_agent_id }) =>
        ({ node_key, title, goal, acceptance_criteria, assigned_agent_id })),
      dependencies: value.dependencies, expected_graph_revision: plan.graph_revision,
    }, { headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${plan.version}"` } }).then(r => r.data),
  revisePlan: (plan: PlanExecution, instruction: string) => client.post<PlanExecution>(
    `/plans/${plan.id}/revise`, { instruction, expected_graph_revision: plan.graph_revision },
    { headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${plan.version}"` } },
  ).then(r => r.data),
  confirmPlan: (plan: PlanExecution) => client.post<PlanExecution>(
    `/plans/${plan.id}/confirm`, { expected_graph_revision: plan.graph_revision },
    { headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${plan.version}"` } },
  ).then(r => r.data),
  cancelPlan: (plan: PlanExecution, reason: string) => client.post<PlanExecution>(
    `/plans/${plan.id}/cancel`, { reason },
    { headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${plan.version}"` } },
  ).then(r => r.data),
  getPlanWorkItems: (planId: string) => client.get<{ items: PlanWorkItem[] }>(
    `/plans/${planId}/work-items`,
  ).then(r => r.data.items),
  listGroupFormations: (sessionId: string) => client.get<{ items: GroupFormationProposal[] }>(
    `/sessions/${sessionId}/group-formations`,
  ).then(r => r.data.items),
  createGroupFormation: (sessionId: string, goal: string) => client.post<GroupFormationProposal>(
    `/sessions/${sessionId}/group-formations`, { goal }, idempotency(),
  ).then(r => r.data),
  updateGroupFormation: (proposal: GroupFormationProposal, revision: GroupFormationRevision) =>
    client.patch<GroupFormationProposal>(`/group-formations/${proposal.id}`, {
      goal: revision.goal, bootstrap_context: revision.bootstrap_context,
      participants: revision.participants.map(({ participant_key, agent_id, is_entry, initial_responsibility }) =>
        ({ participant_key, agent_id, is_entry, initial_responsibility })),
      initial_work_items: revision.initial_work_items.map(({ work_item_key, participant_key, title, goal, acceptance_criteria }) =>
        ({ work_item_key, participant_key, title, goal, acceptance_criteria })),
      suggested_edges: revision.suggested_edges.map(({ source_participant_key, target_participant_key, label, rationale }) =>
        ({ source_participant_key, target_participant_key, label, rationale })),
      expected_revision_no: proposal.current_revision_no,
    }, { headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${proposal.version}"` } }).then(r => r.data),
  confirmGroupFormation: (proposal: GroupFormationProposal) => client.post(
    `/group-formations/${proposal.id}/confirm`, { expected_revision_no: proposal.current_revision_no },
    { headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${proposal.version}"` } },
  ).then(r => r.data),
  rejectGroupFormation: (proposal: GroupFormationProposal) => client.post(
    `/group-formations/${proposal.id}/reject`, {},
    { headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${proposal.version}"` } },
  ).then(r => r.data),
  getGroup: (groupId: string) => client.get(`/groups/${groupId}`).then(r => r.data),
  getGroupNodes: (groupId: string) => client.get<{ items: GroupNode[] }>(`/groups/${groupId}/nodes`).then(r => r.data.items),
  getGroupSuggestedEdges: (groupId: string) => client.get<{ items: GroupSuggestedEdge[] }>(
    `/groups/${groupId}/suggested-edges`,
  ).then(r => r.data.items),
  getGroupCollaborationEdges: (groupId: string) => client.get<{ items: GroupCollaborationEdge[] }>(
    `/groups/${groupId}/collaboration-edges`,
  ).then(r => r.data.items),
  getGroupRuns: (groupId: string) => client.get<{ items: GroupRun[] }>(`/groups/${groupId}/runs`).then(r => r.data.items),
  createGroupRun: (groupId: string, content: string, recipientNodeId?: string) => client.post<GroupRun>(
    `/groups/${groupId}/runs`, { content, recipient_node_id: recipientNodeId, reference_run_ids: [] }, idempotency(),
  ).then(r => r.data),
  getGroupRunMessages: (groupRunId: string) => client.get<{ items: GroupMessage[] }>(
    `/group-runs/${groupRunId}/messages`,
  ).then(r => r.data.items),
  sendGroupRunMessage: (groupRunId: string, content: string, recipientNodeId?: string) => client.post(
    `/group-runs/${groupRunId}/messages`, { content, recipient_node_id: recipientNodeId }, idempotency(),
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
  getRunTimeline: (id: string, afterSequence = 0) => client.get<{
    items: ExecutionEvent[]; scanned_through_sequence: number; session_high_watermark: number; has_more: boolean
  }>(`/runs/${id}/timeline`, { params: { after_sequence: afterSequence } }).then(r => r.data),
  getRunRunners: (id: string) => client.get<{ items: Runner[] }>(`/runs/${id}/runners`).then(r => r.data.items),
  getRunnerTrace: (id: string) => client.get<RunnerTraceView>(`/runners/${id}/trace`).then(r => r.data),
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
  onEvent: (event: ExecutionEvent | LiveDeltaEvent | { event_type: 'cursor'; sequence: number }) => void,
  onConnection: (connected: boolean) => void,
  onCursorReset?: () => void,
): () => void {
  const controller = new AbortController()
  const seenEventIds = new Set<string>()
  const seenEventOrder: string[] = []
  const retryDelays = [1000, 2000, 5000, 10000, 30000]

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
    let cursor = afterSequence
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
            onEvent({ event_type: 'cursor', sequence: cursor })
            hasMore = page.has_more
          } catch (error: any) {
            if (error?.response?.status !== 410) throw error
            cursor = 0
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
            if (id) cursor = Math.max(cursor, Number(id))
            if (eventType === 'cursor' && id) onEvent({ event_type: 'cursor', sequence: cursor })
            else if (data) {
              const event = JSON.parse(data) as ExecutionEvent | LiveDeltaEvent
              if (event.event_type === 'model.delta' && 'delta' in event) onEvent(event)
              else if ('event_id' in event && remember(event)) onEvent(event)
            }
          }
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
