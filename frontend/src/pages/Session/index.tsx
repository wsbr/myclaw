import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Modal, Select, Space, Tag, Tooltip, message as toast } from 'antd'
import { HistoryOutlined } from '@ant-design/icons'
import { Activity, ArrowLeft, GitBranch, MessageSquare, PanelRightOpen, Plus, UsersRound } from 'lucide-react'
import { agentsApi } from '@/api/agents'
import {
  ApprovalAction, LiveToolProgressEvent, Message, ToolConfirmation,
  sessionsApi, subscribeSessionEvents,
} from '@/api/sessions'
import { cn } from '@/lib/utils'
import MessageThread from './MessageThread'
import PendingActions from './PendingActions'
import PlanPanel from './PlanPanel'
import GroupPanel from './GroupPanel'
import SessionComposer from './SessionComposer'
import SessionInspector from './SessionInspector'
import { isTerminalRun, runStatusMeta } from './runtimeView'
import { appendLiveDelta, closeLiveDraft, type LiveDraft } from './liveState'

export default function SessionPage() {
  const { agentId, sessionId: routeSessionId } = useParams<{ agentId: string; sessionId?: string }>()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const [sessionId, setSessionId] = useState<string | null>(routeSessionId ?? null)
  const [input, setInput] = useState('')
  const [mode, setMode] = useState<'chat' | 'plan' | 'group'>('chat')
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [connected, setConnected] = useState(true)
  const [liveDraft, setLiveDraft] = useState<LiveDraft | null>(null)
  const [liveTools, setLiveTools] = useState<Record<string, LiveToolProgressEvent>>({})
  const [inspectorOpen, setInspectorOpen] = useState(() => window.innerWidth >= 1280)
  const [showScrollButton, setShowScrollButton] = useState(false)
  const [clarificationAnswers, setClarificationAnswers] = useState<Record<string, string>>({})
  const [groupRecipient, setGroupRecipient] = useState<string | undefined>()
  const [selectedGroupId, setSelectedGroupId] = useState<string | null>(null)
  const [selectedGroupRunId, setSelectedGroupRunId] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const messageViewportRef = useRef<HTMLElement>(null)
  const followOutputRef = useRef(true)
  const latestRunIdRef = useRef<string | null>(null)
  const closedStreamIdsRef = useRef(new Set<string>())
  const suppressLiveUntilNextRunRef = useRef(Boolean(routeSessionId))

  const { data: agent } = useQuery({
    queryKey: ['agent-summary', agentId], queryFn: () => agentsApi.getSummary(agentId!), enabled: !!agentId,
  })
  const { data: allSessions = [] } = useQuery({ queryKey: ['sessions'], queryFn: sessionsApi.list })
  const sessions = useMemo(
    () => allSessions.filter(item => item.entry_agent_id === agentId),
    [allSessions, agentId],
  )
  const { data: messages = [], isLoading: historyLoading } = useQuery({
    queryKey: ['messages', sessionId], queryFn: () => sessionsApi.getMessages(sessionId!), enabled: !!sessionId,
  })
  const { data: sessionRecord } = useQuery({
    queryKey: ['session', sessionId], queryFn: () => sessionsApi.get(sessionId!), enabled: !!sessionId,
  })
  const { data: planRuns = [] } = useQuery({
    queryKey: ['session-runs', sessionId, 'plan'], queryFn: () => sessionsApi.getSessionRuns(sessionId!, 'plan'), enabled: !!sessionId,
    refetchInterval: query => query.state.data?.some(run => !isTerminalRun(run.status)) ? 2500 : false,
  })
  const activePlanRun = planRuns.find(run => !isTerminalRun(run.status)) || planRuns[0]
  const { data: groups = [] } = useQuery({
    queryKey: ['groups', sessionId], queryFn: () => sessionsApi.listGroups(sessionId!), enabled: !!sessionId,
  })
  const requestedGroupId = searchParams.get('group')
  const activeGroup = groups.find(item => item.id === (selectedGroupId || requestedGroupId)) || groups[0]
  const groupId = activeGroup?.id || null
  const { data: groupMembers = [] } = useQuery({
    queryKey: ['group-memberships', groupId], queryFn: () => sessionsApi.getGroupMemberships(groupId!), enabled: !!groupId,
  })
  const { data: groupRuns = [] } = useQuery({
    queryKey: ['group-runs', groupId], queryFn: () => sessionsApi.getGroupRuns(groupId!), enabled: !!groupId,
    refetchInterval: query => query.state.data?.some(item => !isTerminalRun(item.run.status)) ? 2500 : false,
  })
  const latestGroupRun = groupRuns.find(item => !isTerminalRun(item.run.status)) || groupRuns[0]
  const activeGroupRun = groupRuns.find(item => item.id === selectedGroupRunId) || latestGroupRun
  const groupIsOpen = activeGroup?.status === 'open'
  const { data: groupMessages = [] } = useQuery({
    queryKey: ['group-run-messages', activeGroupRun?.id],
    queryFn: () => sessionsApi.getGroupRunMessages(activeGroupRun!.id), enabled: !!activeGroupRun,
  })
  const { data: interactionGraph } = useQuery({
    queryKey: ['group-interaction-graph', activeGroupRun?.id],
    queryFn: () => sessionsApi.getInteractionGraph(activeGroupRun!.id),
    enabled: !!activeGroupRun,
  })
  const { data: groupDiagnostics = [] } = useQuery({
    queryKey: ['group-diagnostics', activeGroupRun?.id],
    queryFn: () => sessionsApi.getGroupDiagnostics(activeGroupRun!.id),
    enabled: !!activeGroupRun,
    refetchInterval: activeGroupRun && !isTerminalRun(activeGroupRun.run.status) ? 2500 : false,
  })
  const { data: sharedTasks = [] } = useQuery({
    queryKey: ['group-shared-tasks', groupId],
    queryFn: () => sessionsApi.getSharedTasks(groupId!), enabled: !!groupId,
  })
  const { data: groupPlans = [] } = useQuery({
    queryKey: ['group-working-plans', activeGroupRun?.run.id],
    queryFn: () => sessionsApi.getWorkingPlans(activeGroupRun!.run.id), enabled: !!activeGroupRun,
  })
  const activeSharedTasks = sharedTasks.filter(
    task => !task.group_run_id || task.group_run_id === activeGroupRun?.id,
  )
  const { data: networkCards } = useQuery({
    queryKey: ['agent-network', 'group-ui'],
    queryFn: () => agentsApi.searchNetwork({ group_enabled: true, limit: 100 }),
  })
  const latestRunId = activeRunId || (mode === 'plan' ? activePlanRun?.id : null)
    || (mode === 'group' ? activeGroupRun?.run.id : null)
    || [...messages].reverse().find(item => item.run_id)?.run_id || null
  const { data: activeRun } = useQuery({
    queryKey: ['run', latestRunId], queryFn: () => sessionsApi.getRun(latestRunId!), enabled: !!latestRunId,
    refetchInterval: query => query.state.data && !isTerminalRun(query.state.data.status) ? 2000 : false,
  })
  const { data: activeTools = [] } = useQuery({
    queryKey: ['tool-invocations', latestRunId],
    queryFn: () => sessionsApi.getToolInvocations(latestRunId!),
    enabled: !!latestRunId && mode !== 'group',
    refetchInterval: activeRun && !isTerminalRun(activeRun.status) ? 2000 : false,
  })
  const { data: workingPlans = [] } = useQuery({
    queryKey: ['working-plans', latestRunId],
    queryFn: () => sessionsApi.getWorkingPlans(latestRunId!), enabled: !!latestRunId,
    refetchInterval: query => {
      if (!latestRunId || query.state.data?.length) return false
      if (activeRun?.id === latestRunId && isTerminalRun(activeRun.status)) return false
      return 1000
    },
  })
  const activeWorkingPlan = workingPlans.find(plan => plan.status === 'active') || workingPlans[0]
  const { data: agentInvocations = [] } = useQuery({
    queryKey: ['agent-invocations', activePlanRun?.id],
    queryFn: () => sessionsApi.getAgentInvocations(activePlanRun!.id), enabled: !!activePlanRun,
  })
  const { data: artifacts = [] } = useQuery({
    queryKey: ['artifacts', sessionId], queryFn: () => sessionsApi.getArtifacts(sessionId!), enabled: !!sessionId,
  })
  useEffect(() => { latestRunIdRef.current = latestRunId }, [latestRunId])
  const { data: requiredActions = [] } = useQuery({
    queryKey: ['required-actions'], queryFn: sessionsApi.getRequiredActions, refetchInterval: 5000,
  })
  const sessionActions = requiredActions.filter(action => action.session_id === sessionId)
  const toolActions = sessionActions.filter(
    (action): action is ApprovalAction | ToolConfirmation =>
      ['tool', 'session_grant', 'tool_confirmation'].includes(action.type),
  )
  const clarificationActions = sessionActions.filter(action => action.type === 'clarification')
  const pendingActionCount = toolActions.length + clarificationActions.length
  const planApproval = sessionActions.find(
    (action): action is ApprovalAction => action.type === 'plan_implementation' && action.run_id === activePlanRun?.id,
  )

  useEffect(() => {
    setSessionId(routeSessionId ?? null)
    setActiveRunId(null)
    setLiveDraft(null)
    setLiveTools({})
    closedStreamIdsRef.current.clear()
    suppressLiveUntilNextRunRef.current = Boolean(routeSessionId)
    setClarificationAnswers({})
    setSelectedGroupId(null)
    setSelectedGroupRunId(null)
    followOutputRef.current = true
    setShowScrollButton(false)
    setMode(searchParams.get('mode') === 'group' ? 'group' : 'chat')
  }, [routeSessionId, searchParams])

  useEffect(() => {
    if (sessionRecord?.interaction_mode === 'plan') setMode('plan')
  }, [sessionRecord?.interaction_mode])

  useEffect(() => {
    setGroupRecipient(undefined)
    setSelectedGroupRunId(null)
  }, [groupId])

  useEffect(() => {
    if (!sessionId) return
    return subscribeSessionEvents(sessionId, 0, event => {
      if (event.event_type === 'model.delta' && 'stream_id' in event && 'delta' in event) {
        setLiveDraft(current => appendLiveDelta(
          current, event, latestRunIdRef.current,
          suppressLiveUntilNextRunRef.current,
          closedStreamIdsRef.current,
        ))
        return
      }
      if (
        (event.event_type === 'model.stream_reset'
          || event.event_type === 'model.stream_committed')
        && 'stream_id' in event
      ) {
        closedStreamIdsRef.current.add(event.stream_id)
        setLiveDraft(current => closeLiveDraft(current, event.stream_id))
        if (event.event_type === 'model.stream_committed') {
          queryClient.invalidateQueries({ queryKey: ['messages', sessionId] })
        }
        return
      }
      if (event.event_type === 'tool.progress' && 'call_id' in event) {
        if (event.run_id === latestRunIdRef.current) {
          setLiveTools(current => ({ ...current, [event.call_id]: event }))
          queryClient.invalidateQueries({ queryKey: ['tool-invocations', event.run_id] })
        }
        return
      }
      if (event.event_type === 'message.created') {
        setLiveDraft(null)
        queryClient.invalidateQueries({ queryKey: ['messages', sessionId] })
      }
      if (event.event_type.startsWith('tool.') || event.event_type.startsWith('tool_invocation.')) {
        if ('run_id' in event && event.run_id) {
          queryClient.invalidateQueries({ queryKey: ['tool-invocations', event.run_id] })
        }
        if ('entity_id' in event && event.entity_id) {
          setLiveTools(current => Object.fromEntries(
            Object.entries(current).filter(([, item]) => item.tool_invocation_id !== event.entity_id),
          ))
        }
      }
      if (event.event_type.startsWith('tool.confirmation') || event.event_type.startsWith('tool_confirmation.')) {
        queryClient.invalidateQueries({ queryKey: ['required-actions'] })
      }
      if (event.event_type.startsWith('clarification.')) {
        queryClient.invalidateQueries({ queryKey: ['required-actions'] })
        queryClient.invalidateQueries({ queryKey: ['messages', sessionId] })
      }
      if (event.event_type.startsWith('approval.')) {
        queryClient.invalidateQueries({ queryKey: ['required-actions'] })
        queryClient.invalidateQueries({ queryKey: ['session', sessionId] })
      }
      if (event.event_type === 'working_plan.updated') {
        queryClient.invalidateQueries({ queryKey: ['working-plans', event.run_id] })
      }
      if (event.event_type.startsWith('plan.')) {
        queryClient.invalidateQueries({ queryKey: ['session', sessionId] })
        queryClient.invalidateQueries({ queryKey: ['session-runs', sessionId, 'plan'] })
        queryClient.invalidateQueries({ queryKey: ['working-plans'] })
        queryClient.invalidateQueries({ queryKey: ['required-actions'] })
      }
      if (event.event_type.startsWith('group')) {
        if (event.event_type === 'group.emerged' || event.event_type === 'group.run_emerged') {
          const emergedGroupId = event.payload.group_session_id
          const emergedGroupRunId = event.payload.group_run_id
          if (typeof emergedGroupId === 'string') setSelectedGroupId(emergedGroupId)
          if (typeof emergedGroupRunId === 'string') setSelectedGroupRunId(emergedGroupRunId)
          if (event.run_id) setActiveRunId(event.run_id)
          setMode('group')
          toast.info('Chat 已完成，模型选择的 Group Agent 已开始涌现协作')
        }
        queryClient.invalidateQueries({ queryKey: ['groups', sessionId] })
        queryClient.invalidateQueries({ queryKey: ['group-memberships'] })
        queryClient.invalidateQueries({ queryKey: ['group-runs'] })
        queryClient.invalidateQueries({ queryKey: ['group-run-messages'] })
        queryClient.invalidateQueries({ queryKey: ['group-interaction-graph'] })
        queryClient.invalidateQueries({ queryKey: ['group-diagnostics'] })
        queryClient.invalidateQueries({ queryKey: ['group-shared-tasks'] })
        queryClient.invalidateQueries({ queryKey: ['required-actions'] })
      }
      if (event.event_type.startsWith('agent_invocation.')) {
        queryClient.invalidateQueries({ queryKey: ['group-interaction-graph'] })
        if ('run_id' in event) {
          queryClient.invalidateQueries({ queryKey: ['agent-invocations', event.run_id] })
        }
      }
      if (event.event_type.startsWith('artifact.')) {
        queryClient.invalidateQueries({ queryKey: ['artifacts', sessionId] })
      }
      if (event.event_type.startsWith('verification.') && 'run_id' in event) {
        queryClient.invalidateQueries({ queryKey: ['verification-results', event.run_id] })
      }
      queryClient.invalidateQueries({ queryKey: ['session-inspector-events', sessionId] })
      if ('run_id' in event && event.run_id) {
        queryClient.invalidateQueries({ queryKey: ['run', event.run_id] })
        queryClient.invalidateQueries({ queryKey: ['run-timeline', event.run_id] })
        queryClient.invalidateQueries({ queryKey: ['run-runners', event.run_id] })
        queryClient.invalidateQueries({ queryKey: ['session-runs', sessionId] })
      }
      if (['run.completed', 'run.cancelled', 'run.failed'].includes(event.event_type) && 'run_id' in event) {
        queryClient.invalidateQueries({ queryKey: ['run', event.run_id] })
        setLiveTools(current => Object.fromEntries(
          Object.entries(current).filter(([, item]) => item.run_id !== event.run_id),
        ))
        if (event.event_type === 'run.failed') toast.error(`Agent 执行失败：${String(event.payload.error_code || 'unknown')}`)
      }
    }, value => {
      setConnected(value)
      if (!value) {
        suppressLiveUntilNextRunRef.current = true
        setLiveDraft(null)
      }
    }, () => {
      setLiveDraft(null)
      setLiveTools({})
      closedStreamIdsRef.current.clear()
      suppressLiveUntilNextRunRef.current = true
      queryClient.invalidateQueries({ queryKey: ['messages', sessionId] })
      queryClient.invalidateQueries({ queryKey: ['session', sessionId] })
      queryClient.invalidateQueries({ queryKey: ['session-runs', sessionId] })
      queryClient.invalidateQueries({ queryKey: ['groups', sessionId] })
      queryClient.invalidateQueries({ queryKey: ['required-actions'] })
      queryClient.invalidateQueries({ queryKey: ['session-inspector-events', sessionId] })
      if (latestRunIdRef.current) {
        queryClient.invalidateQueries({ queryKey: ['run-timeline', latestRunIdRef.current] })
        queryClient.invalidateQueries({ queryKey: ['run-runners', latestRunIdRef.current] })
      }
    })
  }, [sessionId, queryClient])

  const scrollToBottom = (behavior: ScrollBehavior = 'smooth') => {
    followOutputRef.current = true
    setShowScrollButton(false)
    bottomRef.current?.scrollIntoView({ behavior, block: 'end' })
  }

  useEffect(() => {
    if (followOutputRef.current) scrollToBottom(liveDraft?.text ? 'auto' : 'smooth')
  }, [messages.length, liveDraft?.text, liveTools])

  const ensureSession = async () => {
    if (sessionId) return sessionId
    const created = await sessionsApi.create(agentId!)
    setSessionId(created.id)
    navigate(`/agents/${agentId}/chat/${created.id}`, { replace: true })
    queryClient.invalidateQueries({ queryKey: ['sessions'] })
    return created.id
  }

  const cacheMessage = (item: Message) => {
    queryClient.setQueryData<Message[]>(['messages', item.session_id], current => {
      if (!current?.length) return [item]
      if (current.some(existing => existing.id === item.id)) return current
      return [...current, item]
    })
  }

  const send = useMutation({
    mutationFn: async (content: string) => {
      const id = await ensureSession()
      return sessionsApi.sendMessage(id, content)
    },
    onSuccess: result => {
      setActiveRunId(result.run.id)
      suppressLiveUntilNextRunRef.current = false
      queryClient.setQueryData(['run', result.run.id], result.run)
      cacheMessage(result.message)
      setLiveDraft(null)
      setLiveTools({})
      queryClient.invalidateQueries({ queryKey: ['messages', result.message.session_id] })
    },
    onError: (error: any) => toast.error(error?.response?.data?.error?.message || '消息提交失败'),
  })
  const sendPlan = useMutation({
    mutationFn: async (goal: string) => {
      const id = await ensureSession()
      const current = await sessionsApi.get(id)
      return sessionsApi.enterPlanMode(current, goal)
    },
    onSuccess: result => {
      setActiveRunId(result.run.id)
      suppressLiveUntilNextRunRef.current = false
      setLiveDraft(null)
      setLiveTools({})
      cacheMessage(result.message)
      queryClient.setQueryData(['run', result.run.id], result.run)
      queryClient.setQueryData(['session', result.session.id], result.session)
      queryClient.invalidateQueries({ queryKey: ['messages', result.session.id] })
      queryClient.invalidateQueries({ queryKey: ['session-runs', result.session.id, 'plan'] })
    },
    onError: (error: any) => toast.error(error?.response?.data?.error?.code || 'Plan 创建失败'),
  })
  const sendGroup = useMutation({
    mutationFn: async (content: string) => {
      await ensureSession()
      if (!activeGroup) throw new Error('请先在 Chat 中完成一次对话，系统会自动形成 Group')
      if (!groupIsOpen) throw new Error('Group 已关闭，只能读取历史记录')
      if (latestGroupRun && !['completed', 'cancelled', 'failed'].includes(latestGroupRun.run.status)) {
        return { kind: 'message' as const, value: await sessionsApi.sendGroupRunMessage(latestGroupRun.id, content, groupRecipient) }
      }
      return { kind: 'run' as const, value: await sessionsApi.createGroupRun(activeGroup.id, content, groupRecipient) }
    },
    onSuccess: result => {
      if (result.kind === 'run') {
        setActiveRunId(result.value.run.id)
        setSelectedGroupRunId(result.value.id)
      }
      setLiveDraft(null)
      setLiveTools({})
      queryClient.invalidateQueries({ queryKey: ['group-runs'] })
      queryClient.invalidateQueries({ queryKey: ['group-run-messages'] })
    },
    onError: (error: any) => toast.error(error?.response?.data?.error?.code || error.message || 'Group 请求失败'),
  })

  const refreshPlan = () => {
    queryClient.invalidateQueries({ queryKey: ['session', sessionId] })
    queryClient.invalidateQueries({ queryKey: ['session-runs', sessionId, 'plan'] })
    queryClient.invalidateQueries({ queryKey: ['working-plans'] })
    queryClient.invalidateQueries({ queryKey: ['agent-invocations'] })
    queryClient.invalidateQueries({ queryKey: ['required-actions'] })
  }
  const revisePlan = useMutation({
    mutationFn: (instruction: string) => sessionsApi.continuePlanning(activeWorkingPlan!, instruction),
    onSuccess: result => {
      setActiveRunId(result.run.id)
      suppressLiveUntilNextRunRef.current = false
      cacheMessage(result.message)
      queryClient.setQueryData(['run', result.run.id], result.run)
      queryClient.setQueryData(['session', result.session.id], result.session)
      queryClient.invalidateQueries({ queryKey: ['messages', result.message.session_id] })
      refreshPlan()
    },
    onError: (error: any) => toast.error(error?.response?.data?.detail?.code || 'Plan 修改方向失败'),
  })
  const confirmPlan = useMutation({
    mutationFn: () => sessionsApi.approvePlanImplementation(activeWorkingPlan!, planApproval!.subject_hash),
    onSuccess: () => {
      suppressLiveUntilNextRunRef.current = false
      refreshPlan(); toast.success('当前 revision 已批准；同一 Session 开始动态实施')
    },
    onError: (error: any) => toast.error(error?.response?.data?.detail?.code || 'Plan 批准失败'),
  })
  const rejectPlan = useMutation({
    mutationFn: (reason?: string) => sessionsApi.decideApproval(planApproval!, 'reject', reason),
    onSuccess: () => {
      suppressLiveUntilNextRunRef.current = false
      refreshPlan(); toast.info('当前 Plan revision 已拒绝，项目经理将继续只读规划')
    },
    onError: (error: any) => toast.error(error?.response?.data?.detail?.code || 'Plan 拒绝失败'),
  })
  const exitPlan = useMutation({
    mutationFn: () => sessionsApi.exitPlanMode(sessionRecord!),
    onSuccess: result => {
      queryClient.setQueryData(['session', result.id], result)
      setActiveRunId(null); setMode('chat'); refreshPlan()
    },
    onError: (error: any) => toast.error(error?.response?.data?.detail?.code || '退出 Plan 失败'),
  })
  const replaceReporter = useMutation({
    mutationFn: (agentId: string) => sessionsApi.replaceGroupReporter(activeGroupRun!, agentId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['group-runs', groupId] })
      toast.success('Reporter 已替换，将重新评估自然收口')
    },
    onError: (error: any) => toast.error(error?.response?.data?.detail?.code || 'Reporter 替换失败'),
  })
  const closeGroup = useMutation({
    mutationFn: () => sessionsApi.closeGroup(
      activeGroup!.id,
      activeGroup!.version,
      '用户从 Web 工作区关闭群组',
    ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['groups', sessionId] })
      queryClient.invalidateQueries({ queryKey: ['group-runs'] })
      toast.info('Group 已关闭并转为只读')
    },
    onError: (error: any) => toast.error(error?.response?.data?.error?.code || 'Group 关闭失败'),
  })

  const cancel = useMutation({
    mutationFn: async () => {
      if (mode === 'group' && activeGroupRun) {
        const target = latestGroupRun || activeGroupRun
        return { run: (await sessionsApi.cancelGroupRun(target, '用户在 Web 工作区取消')).run }
      }
      return { run: await sessionsApi.cancelRun(activeRun!, '用户在 Web 工作区取消') }
    },
    onSuccess: result => {
      const run = result.run
      setActiveRunId(null)
      suppressLiveUntilNextRunRef.current = true
      setLiveDraft(null)
      setLiveTools({})
      queryClient.setQueryData(['run', run.id], run)
      refreshPlan()
      toast.info('Run 已取消；已发生的外部副作用不会回滚')
    },
    onError: (error: any) => toast.error(error?.response?.data?.error?.code || '取消失败'),
  })
  const decide = useMutation<
    ToolConfirmation | ApprovalAction,
    unknown,
    { action: ToolConfirmation | ApprovalAction; decision: 'approve' | 'reject' }
  >({
    mutationFn: ({ action, decision }: { action: typeof toolActions[number]; decision: 'approve' | 'reject' }) =>
      action.type === 'tool_confirmation'
        ? sessionsApi.decideToolConfirmation(action, decision)
        : sessionsApi.decideApproval(action as ApprovalAction, decision),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['required-actions'] })
      if (latestRunId) queryClient.invalidateQueries({ queryKey: ['run', latestRunId] })
    },
    onError: (error: any) => toast.error(error?.response?.data?.error?.code || '操作失败'),
  })
  const answerClarification = useMutation({
    mutationFn: ({ action, answer }: { action: typeof clarificationActions[number]; answer: string }) =>
      sessionsApi.answerClarification(action, answer),
    onSuccess: result => {
      setClarificationAnswers(value => ({ ...value, [result.id]: '' }))
      queryClient.invalidateQueries({ queryKey: ['required-actions'] })
      queryClient.invalidateQueries({ queryKey: ['messages', sessionId] })
      queryClient.invalidateQueries({ queryKey: ['run', result.run_id] })
    },
    onError: (error: any) => toast.error(error?.response?.data?.error?.code || '回答提交失败'),
  })

  const isRunning = Boolean(activeRun && !isTerminalRun(activeRun.status))
  const isActivelyExecuting = Boolean(activeRun && ['active', 'waiting_tool', 'waiting_agent'].includes(activeRun.status))
  const planBusy = sendPlan.isPending || revisePlan.isPending || confirmPlan.isPending
    || rejectPlan.isPending || exitPlan.isPending
  const groupBusy = replaceReporter.isPending || closeGroup.isPending
  const planAlreadyEntered = sessionRecord?.interaction_mode === 'plan'
  const canRevisePlan = Boolean(
    planAlreadyEntered
    && sessionRecord?.permission_mode === 'plan_readonly'
    && activeWorkingPlan
    && activePlanRun
    && ['waiting_approval', 'waiting_user'].includes(activePlanRun.status),
  )
  const submitting = send.isPending || sendPlan.isPending || sendGroup.isPending || revisePlan.isPending
  const inputDisabled = submitting
    || (isRunning && mode !== 'group' && !(mode === 'plan' && canRevisePlan))
    || (mode !== 'plan' && Boolean(planAlreadyEntered))
    || (mode === 'plan' && Boolean(planAlreadyEntered) && !canRevisePlan && !isActivelyExecuting)
    || (mode === 'group' && Boolean(!activeGroup || !groupIsOpen))

  const submitComposer = () => {
    const content = input.trim()
    if (!content || inputDisabled) return
    setInput('')
    const restore = () => setInput(current => current || content)
    if (mode !== 'group' && canRevisePlan && activeWorkingPlan) {
      revisePlan.mutate(content, { onError: restore })
      return
    }
    if (mode === 'plan') {
      sendPlan.mutate(content, { onError: restore })
      return
    }
    if (mode === 'group') sendGroup.mutate(content, { onError: restore })
    else send.mutate(content, { onError: restore })
  }

  const requestCancel = () => Modal.confirm({
    title: '停止本次运行？',
    content: '停止会阻止后续执行，但不会回滚已经完成的工具调用或外部副作用。',
    okText: '确认停止',
    okButtonProps: { danger: true },
    cancelText: '继续执行',
    onOk: () => cancel.mutateAsync(),
  })

  const selectMode = (nextMode: 'chat' | 'plan' | 'group') => {
    if (nextMode === mode) return
    if (planAlreadyEntered && nextMode !== 'plan') {
      toast.info('请先退出当前 Plan，再切换到其他模式')
      return
    }
    setMode(nextMode)
    setActiveRunId(null)
    setLiveDraft(null)
    setLiveTools({})
    followOutputRef.current = true
  }

  const currentRunMeta = activeRun ? runStatusMeta[activeRun.status] : null

  return (
    <div className="relative flex h-full min-h-0 overflow-hidden bg-background">
      <aside className="hidden w-56 shrink-0 flex-col border-r border-sidebar-border bg-sidebar lg:flex">
        <div className="space-y-2 border-b p-3">
          <Button icon={<ArrowLeft className="size-3.5" />} block onClick={() => navigate('/agents')}>返回 Agents</Button>
          <Button type="primary" icon={<Plus className="size-3.5" />} block onClick={() => navigate(`/agents/${agentId}/chat`)}>新对话</Button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          <div className="mb-2 flex items-center justify-between px-1">
            <p className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">历史会话</p>
            <span className="text-[10px] tabular-nums text-muted-foreground">{sessions.length}</span>
          </div>
          {sessions.map(item => (
            <button key={item.id} type="button" onClick={() => navigate(`/agents/${agentId}/chat/${item.id}`)}
              className={cn(
                'mb-1 flex w-full items-center gap-2 rounded-xl px-2.5 py-2.5 text-left text-sm transition-colors',
                item.id === sessionId ? 'bg-sidebar-accent font-medium text-sidebar-accent-foreground' : 'text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground',
              )}>
              <HistoryOutlined className="shrink-0" />
              <span className="min-w-0 flex-1 truncate">{item.title || item.id.slice(0, 12)}</span>
              {item.interaction_mode === 'plan' && <span className="size-1.5 shrink-0 rounded-full bg-violet-500" title="Plan Session" />}
            </button>
          ))}
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b bg-background/85 px-4 backdrop-blur-lg">
          <div className="flex min-w-0 items-center gap-3">
            <div className="hidden min-w-0 md:block">
              <p className="truncate text-sm font-semibold">{agent?.name || 'Agent'}</p>
              <p className="truncate text-[10px] text-muted-foreground">{sessionRecord?.title || (sessionId ? `Session ${sessionId.slice(0, 8)}` : '新会话')}</p>
            </div>
            <Button.Group className="flex">
              <Button size="small" icon={<MessageSquare className="size-3.5" />} disabled={Boolean(planAlreadyEntered)} type={mode === 'chat' ? 'primary' : 'default'} onClick={() => selectMode('chat')}>Chat</Button>
              <Button size="small" icon={<GitBranch className="size-3.5" />} type={mode === 'plan' ? 'primary' : 'default'} onClick={() => selectMode('plan')}>Plan</Button>
              {(agent?.group_enabled || groups.length > 0) && <Button size="small" icon={<UsersRound className="size-3.5" />} disabled={Boolean(planAlreadyEntered)} type={mode === 'group' ? 'primary' : 'default'} onClick={() => selectMode('group')}>Group</Button>}
            </Button.Group>
          </div>
          <Space size={6} className="shrink-0">
            {sessionRecord && <Tag color={sessionRecord.permission_mode === 'plan_readonly' ? 'gold' : 'green'} className="hidden md:inline-flex">
              {sessionRecord.permission_mode} · r{sessionRecord.permission_revision}
            </Tag>}
            {currentRunMeta && <Tag color={currentRunMeta.tone === 'danger' ? 'red' : currentRunMeta.tone === 'success' ? 'green' : currentRunMeta.tone === 'warning' ? 'orange' : 'blue'}>
              {currentRunMeta.label}
            </Tag>}
            {isRunning && <Button danger size="small" loading={cancel.isPending} onClick={requestCancel}>停止</Button>}
            {mode === 'group' && groupIsOpen && <Button danger size="small" loading={closeGroup.isPending} onClick={() => Modal.confirm({
              title: '关闭整个 Group？',
              content: '所有非终态 GroupRun 会被取消，Group 之后只允许读取历史；此操作不同于仅停止当前 Run。',
              okText: '关闭 Group', okButtonProps: { danger: true }, onOk: () => closeGroup.mutateAsync(),
            })}>关闭 Group</Button>}
            {!inspectorOpen && <Tooltip title="打开运行监控"><Button size="small" aria-label="打开运行监控" icon={<PanelRightOpen className="size-4" />} onClick={() => setInspectorOpen(true)} /></Tooltip>}
          </Space>
        </header>

        {!connected && <Alert banner type="warning" showIcon message="实时连接暂时中断，任务仍在后台执行；正在自动恢复进度。" />}
        {activeRun?.status === 'failed' && <Alert banner type="error" showIcon
          message={`运行失败：${activeRun.terminal_reason || 'unknown'}`}
          description="右侧运行监控保留失败、权限拒绝、超时与迟到结果的可靠事件。" />}
        {activeRun?.status === 'cancelled' && <Alert banner type="warning" showIcon message={`运行已停止：${activeRun.terminal_reason || 'cancelled'}`} />}
        {activeRun?.status === 'completed' && activeRun.terminal_reason === 'completed_with_errors' && <Alert
          banner type="warning" showIcon
          message="Group 已完成，但本次 Run 记录了成员或调用失败"
          description="最终摘要必须保留这些失败；可在当前 Run 的图和运行监控中核对。"
        />}

        <section
          ref={messageViewportRef}
          className="min-h-0 flex-1 overflow-y-auto scroll-smooth px-4 py-6"
          onScroll={event => {
            const node = event.currentTarget
            const distance = node.scrollHeight - node.scrollTop - node.clientHeight
            followOutputRef.current = distance < 140
            setShowScrollButton(distance >= 140)
          }}
        >
          <div className={cn('mx-auto w-full', mode === 'group' ? 'max-w-5xl' : 'max-w-3xl')}>
            {mode === 'group' ? <div className="space-y-6">
              <GroupPanel group={activeGroup} members={groupMembers} runs={groupRuns}
                activeRun={activeGroupRun}
                messages={groupMessages} graph={interactionGraph} diagnostics={groupDiagnostics} cards={networkCards?.items || []}
                plans={groupPlans} artifacts={artifacts} requiredActions={sessionActions}
                sharedTasks={activeSharedTasks} busy={groupBusy}
                onSelectRun={value => {
                  setSelectedGroupRunId(value)
                  setActiveRunId(groupRuns.find(item => item.id === value)?.run.id || null)
                }}
                onReplaceReporter={async value => { await replaceReporter.mutateAsync(value) }}
                onDownload={sessionsApi.downloadArtifact} />
            </div> : <div className="space-y-6">
              {mode === 'plan' && <PlanPanel
                session={sessionRecord}
                run={activePlanRun}
                plan={activeWorkingPlan}
                collaborators={agentInvocations}
                approval={planApproval}
                artifacts={artifacts}
                busy={planBusy}
                onApprove={async () => { await confirmPlan.mutateAsync() }}
                onReject={async reason => { await rejectPlan.mutateAsync(reason) }}
                onExit={async () => { await exitPlan.mutateAsync() }}
                onDownload={sessionsApi.downloadArtifact}
              />}

              {mode === 'chat' && activeWorkingPlan && <details open className="overflow-hidden rounded-2xl border border-violet-200 bg-card shadow-sm">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-3 bg-violet-50/60 px-4 py-3 dark:bg-violet-950/15">
                  <span className="flex items-center gap-2"><GitBranch className="size-4 text-violet-500" /><span className="font-semibold">WorkingPlan</span><Tag color="purple">revision {activeWorkingPlan.revision}</Tag></span>
                  <span className="text-xs text-muted-foreground">{activeWorkingPlan.items.filter(item => item.status === 'completed').length}/{activeWorkingPlan.items.length}</span>
                </summary>
                <div className="space-y-2 p-4">
                  <p className="text-sm leading-6 text-muted-foreground">{activeWorkingPlan.summary}</p>
                  {activeWorkingPlan.items.map((item, index) => <div key={`${index}-${item.text}`} className="flex items-start gap-2 text-sm">
                    <span className={cn('mt-2 size-1.5 shrink-0 rounded-full', item.status === 'completed' ? 'bg-emerald-500' : item.status === 'in_progress' ? 'animate-pulse bg-blue-500' : 'bg-slate-300')} />
                    <span className={item.status === 'completed' ? 'text-muted-foreground' : ''}>{item.text}</span>
                  </div>)}
                </div>
              </details>}

              <MessageThread
                agentName={agent?.name}
                messages={messages}
                loading={historyLoading}
                running={isActivelyExecuting}
                liveText={liveDraft?.text || ''}
                activeTools={activeTools}
                liveTools={Object.values(liveTools).filter(item => item.run_id === latestRunId)}
                showScrollButton={showScrollButton}
                onScrollToBottom={() => scrollToBottom()}
              />
            </div>}
            <div ref={bottomRef} className="h-1" />
          </div>
        </section>

        <footer className="shrink-0 border-t bg-background/90 px-4 pb-4 pt-3 backdrop-blur-lg">
          <div className={cn('mx-auto w-full', mode === 'group' ? 'max-w-5xl' : 'max-w-3xl')}>
            {pendingActionCount > 0 && <div
              className="mb-3 max-h-[min(44vh,30rem)] overflow-y-auto overscroll-contain pr-1"
              aria-live="polite"
            >
              <PendingActions
                toolActions={toolActions}
                clarifications={clarificationActions}
                answers={clarificationAnswers}
                deciding={decide.isPending}
                answering={answerClarification.isPending}
                onAnswerChange={(id, value) => setClarificationAnswers(current => ({ ...current, [id]: value }))}
                onDecision={(action, decision) => decide.mutate({ action, decision })}
                onAnswer={(action, answer) => answerClarification.mutate({ action, answer })}
              />
            </div>}
            <div className="mx-auto max-w-3xl">
              <SessionComposer
                mode={mode}
                value={input}
                disabled={inputDisabled}
                submitting={submitting}
                running={isActivelyExecuting}
                planEntered={Boolean(planAlreadyEntered)}
                hasPlan={Boolean(activeWorkingPlan)}
                planCanRevise={canRevisePlan}
                groupReady={Boolean(activeGroup && groupIsOpen)}
                accessory={mode === 'group' && activeGroup ? <Select
                  className="w-48 shrink-0"
                  value={groupRecipient ?? ''}
                  onChange={value => setGroupRecipient(value || undefined)}
                  disabled={!groupIsOpen}
                  options={[
                    { value: '', label: 'Reporter（默认）' },
                    ...groupMembers.filter(member => member.status === 'joined').map(member => ({
                      value: member.agent_id,
                      label: networkCards?.items.find(card => card.agent_id === member.agent_id)?.name || member.agent_id.slice(0, 10),
                    })),
                  ]}
                  aria-label="Group 接收 Agent"
                /> : undefined}
                onChange={setInput}
                onSubmit={submitComposer}
                onStop={requestCancel}
              />
            </div>
          </div>
        </footer>
      </main>

      {inspectorOpen && <SessionInspector
        sessionId={sessionId}
        runId={latestRunId}
        run={activeRun}
        plan={activeWorkingPlan}
        collaborators={mode === 'plan' ? agentInvocations : []}
        onClose={() => setInspectorOpen(false)}
      />}
    </div>
  )
}
