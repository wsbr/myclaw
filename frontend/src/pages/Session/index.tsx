import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Input, Modal, Select, Space, Spin, Tag, message as toast } from 'antd'
import { ArrowLeftOutlined, HistoryOutlined, SendOutlined, StopOutlined } from '@ant-design/icons'
import { agentsApi } from '@/api/agents'
import { ApprovalAction, Message, ToolConfirmation, sessionsApi, subscribeSessionEvents } from '@/api/sessions'
import { cn } from '@/lib/utils'
import PlanPanel from './PlanPanel'
import GroupPanel from './GroupPanel'
import SessionInspector from './SessionInspector'

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
  const [liveText, setLiveText] = useState('')
  const [clarificationAnswers, setClarificationAnswers] = useState<Record<string, string>>({})
  const [groupRecipient, setGroupRecipient] = useState<string | undefined>()
  const bottomRef = useRef<HTMLDivElement>(null)
  const latestRunIdRef = useRef<string | null>(null)

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
    refetchInterval: query => query.state.data?.some(run => !['completed', 'cancelled', 'failed'].includes(run.status)) ? 2500 : false,
  })
  const activePlanRun = planRuns.find(run => !['completed', 'cancelled', 'failed'].includes(run.status)) || planRuns[0]
  const { data: groups = [] } = useQuery({
    queryKey: ['groups', sessionId], queryFn: () => sessionsApi.listGroups(sessionId!), enabled: !!sessionId,
  })
  const requestedGroupId = searchParams.get('group')
  const activeGroup = groups.find(item => item.id === requestedGroupId) || groups[0]
  const groupId = activeGroup?.id || null
  const { data: groupMembers = [] } = useQuery({
    queryKey: ['group-memberships', groupId], queryFn: () => sessionsApi.getGroupMemberships(groupId!), enabled: !!groupId,
  })
  const { data: groupRuns = [] } = useQuery({
    queryKey: ['group-runs', groupId], queryFn: () => sessionsApi.getGroupRuns(groupId!), enabled: !!groupId,
    refetchInterval: query => query.state.data?.some(item => ['running', 'waiting', 'settling'].includes(item.run.lifecycle)) ? 2500 : false,
  })
  const activeGroupRun = groupRuns.find(item => !['completed', 'cancelled', 'failed'].includes(item.run.status)) || groupRuns[0]
  const groupIsOpen = activeGroup?.status === 'open'
  const groupRunSettling = activeGroupRun?.run.lifecycle === 'settling'
  const { data: groupMessages = [] } = useQuery({
    queryKey: ['group-messages', groupId],
    queryFn: () => sessionsApi.getGroupMessages(groupId!), enabled: !!groupId,
  })
  const groupGraphQueries = useQueries({
    queries: groupRuns.map(groupRun => ({
      queryKey: ['group-interaction-graph', groupRun.id],
      queryFn: () => sessionsApi.getInteractionGraph(groupRun.id),
      enabled: !!groupId,
    })),
  })
  const interactionGraph = useMemo(() => {
    const nodes = new Map<string, NonNullable<(typeof groupGraphQueries)[number]['data']>['nodes'][number]>()
    const edges = new Map<string, NonNullable<(typeof groupGraphQueries)[number]['data']>['edges'][number]>()
    groupGraphQueries.forEach(query => {
      query.data?.nodes.forEach(node => nodes.set(node.agent_id, node))
      query.data?.edges.forEach(edge => edges.set(`${edge.cause_type}:${edge.cause_id}`, edge))
    })
    return { nodes: [...nodes.values()], edges: [...edges.values()] }
  }, [groupGraphQueries])
  const { data: sharedTasks = [] } = useQuery({
    queryKey: ['group-shared-tasks', groupId],
    queryFn: () => sessionsApi.getSharedTasks(groupId!), enabled: !!groupId,
  })
  const { data: groupPlans = [] } = useQuery({
    queryKey: ['group-working-plans', activeGroupRun?.run.id],
    queryFn: () => sessionsApi.getWorkingPlans(activeGroupRun!.run.id), enabled: !!activeGroupRun,
  })
  const { data: networkCards } = useQuery({
    queryKey: ['agent-network', 'group-ui'],
    queryFn: () => agentsApi.searchNetwork({ group_enabled: true, limit: 100 }),
  })
  const latestRunId = activeRunId || (mode === 'plan' ? activePlanRun?.id : null)
    || (mode === 'group' ? activeGroupRun?.run.id : null)
    || [...messages].reverse().find(item => item.run_id)?.run_id || null
  const { data: activeRun } = useQuery({
    queryKey: ['run', latestRunId], queryFn: () => sessionsApi.getRun(latestRunId!), enabled: !!latestRunId,
    refetchInterval: query => ['running', 'waiting', 'settling'].includes(query.state.data?.lifecycle || '') ? 2000 : false,
  })
  const { data: workingPlans = [] } = useQuery({
    queryKey: ['working-plans', latestRunId],
    queryFn: () => sessionsApi.getWorkingPlans(latestRunId!), enabled: !!latestRunId,
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
  const planApproval = sessionActions.find(
    (action): action is ApprovalAction => action.type === 'plan_implementation' && action.run_id === activePlanRun?.id,
  )

  useEffect(() => {
    setSessionId(routeSessionId ?? null)
    setActiveRunId(null)
    setMode(searchParams.get('mode') === 'group' ? 'group' : 'chat')
  }, [routeSessionId, searchParams])

  useEffect(() => {
    if (sessionRecord?.interaction_mode === 'plan') setMode('plan')
  }, [sessionRecord?.interaction_mode])

  useEffect(() => { setGroupRecipient(undefined) }, [groupId])

  useEffect(() => {
    if (!sessionId) return
    return subscribeSessionEvents(sessionId, 0, event => {
      if (event.event_type === 'model.delta' && 'delta' in event) {
        if (event.run_id === latestRunIdRef.current) setLiveText(value => value + event.delta)
        return
      }
      if (event.event_type === 'message.created') {
        setLiveText('')
        queryClient.invalidateQueries({ queryKey: ['messages', sessionId] })
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
        queryClient.invalidateQueries({ queryKey: ['groups', sessionId] })
        queryClient.invalidateQueries({ queryKey: ['group-memberships'] })
        queryClient.invalidateQueries({ queryKey: ['group-runs'] })
        queryClient.invalidateQueries({ queryKey: ['group-messages'] })
        queryClient.invalidateQueries({ queryKey: ['group-interaction-graph'] })
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
      queryClient.invalidateQueries({ queryKey: ['session-inspector-events', sessionId] })
      if ('run_id' in event && event.run_id) {
        queryClient.invalidateQueries({ queryKey: ['run', event.run_id] })
        queryClient.invalidateQueries({ queryKey: ['session-runs', sessionId] })
      }
      if ((event.event_type === 'run.completed' || event.event_type === 'run.cancelled') && 'run_id' in event) {
        queryClient.invalidateQueries({ queryKey: ['run', event.run_id] })
        setActiveRunId(null)
        if (event.event_type === 'run.completed' && event.payload.outcome === 'failed') toast.error('Agent 执行失败，请查看 Timeline')
      }
    }, value => { setConnected(value); if (!value) setLiveText('') }, () => {
      setLiveText('')
      queryClient.invalidateQueries({ queryKey: ['messages', sessionId] })
      queryClient.invalidateQueries({ queryKey: ['session', sessionId] })
      queryClient.invalidateQueries({ queryKey: ['session-runs', sessionId] })
      queryClient.invalidateQueries({ queryKey: ['groups', sessionId] })
      queryClient.invalidateQueries({ queryKey: ['required-actions'] })
      queryClient.invalidateQueries({ queryKey: ['session-inspector-events', sessionId] })
    })
  }, [sessionId, queryClient])

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages])

  const ensureSession = async () => {
    if (sessionId) return sessionId
    const created = await sessionsApi.create(agentId!)
    setSessionId(created.id)
    navigate(`/agents/${agentId}/chat/${created.id}`, { replace: true })
    queryClient.invalidateQueries({ queryKey: ['sessions'] })
    return created.id
  }

  const send = useMutation({
    mutationFn: async (content: string) => {
      const id = await ensureSession()
      return sessionsApi.sendMessage(id, content)
    },
    onSuccess: result => {
      setActiveRunId(result.run.id)
      setLiveText('')
      setInput('')
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
      setLiveText('')
      setInput('')
      queryClient.setQueryData(['session', result.session.id], result.session)
      queryClient.invalidateQueries({ queryKey: ['messages', result.session.id] })
      queryClient.invalidateQueries({ queryKey: ['session-runs', result.session.id, 'plan'] })
    },
    onError: (error: any) => toast.error(error?.response?.data?.error?.code || 'Plan 创建失败'),
  })
  const sendGroup = useMutation({
    mutationFn: async (content: string) => {
      await ensureSession()
      if (!activeGroup) throw new Error('请先从 Agent Network 选择三名成员直接建组')
      if (!groupIsOpen) throw new Error('Group 已关闭，只能读取历史记录')
      if (groupRunSettling) throw new Error('当前 GroupRun 正在结算，请稍后再提交')
      if (activeGroupRun && ['running', 'waiting'].includes(activeGroupRun.run.lifecycle)) {
        return { kind: 'message' as const, value: await sessionsApi.sendGroupRunMessage(activeGroupRun.id, content, groupRecipient) }
      }
      return { kind: 'run' as const, value: await sessionsApi.createGroupRun(activeGroup.id, content, groupRecipient) }
    },
    onSuccess: result => {
      if (result.kind === 'run') setActiveRunId(result.value.run.id)
      setInput(''); setLiveText('')
      queryClient.invalidateQueries({ queryKey: ['group-runs'] })
      queryClient.invalidateQueries({ queryKey: ['group-messages'] })
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
    onSuccess: result => { setActiveRunId(result.run.id); setInput(''); refreshPlan() },
    onError: (error: any) => toast.error(error?.response?.data?.detail?.code || 'Plan 修改方向失败'),
  })
  const confirmPlan = useMutation({
    mutationFn: () => sessionsApi.approvePlanImplementation(activeWorkingPlan!, planApproval!.subject_hash),
    onSuccess: () => { refreshPlan(); toast.success('当前 revision 已批准；同一 Session 开始动态实施') },
    onError: (error: any) => toast.error(error?.response?.data?.detail?.code || 'Plan 批准失败'),
  })
  const rejectPlan = useMutation({
    mutationFn: (reason?: string) => sessionsApi.decideApproval(planApproval!, 'reject', reason),
    onSuccess: () => { refreshPlan(); toast.info('当前 Plan revision 已拒绝，项目经理将继续只读规划') },
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
        return { run: (await sessionsApi.cancelGroupRun(activeGroupRun, '用户在 Web 工作区取消')).run }
      }
      return { run: await sessionsApi.cancelRun(activeRun!, '用户在 Web 工作区取消') }
    },
    onSuccess: result => {
      const run = result.run
      setActiveRunId(null)
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

  const isRunning = Boolean(activeRun && ['running', 'waiting', 'settling'].includes(activeRun.lifecycle))
  const planBusy = sendPlan.isPending || revisePlan.isPending || confirmPlan.isPending
    || rejectPlan.isPending || exitPlan.isPending
  const groupBusy = replaceReporter.isPending || closeGroup.isPending
  const planAlreadyEntered = sessionRecord?.interaction_mode === 'plan'
  const inputDisabled = send.isPending || sendPlan.isPending || sendGroup.isPending
    || (isRunning && mode !== 'group')
    || (mode === 'plan' && Boolean(planAlreadyEntered))
    || (mode === 'group' && Boolean(!activeGroup || groupRunSettling || !groupIsOpen))

  return (
    <div className="bg-background flex h-full min-h-[calc(100vh-0px)]">
      <aside className="bg-sidebar flex w-60 shrink-0 flex-col border-r border-sidebar-border">
        <div className="space-y-2 border-b p-3">
          <Button icon={<ArrowLeftOutlined />} block onClick={() => navigate('/agents')}>返回 Agents</Button>
          <Button type="primary" block onClick={() => navigate(`/agents/${agentId}/chat`)}>新对话</Button>
        </div>
        <div className="flex-1 overflow-y-auto p-3">
          <p className="text-muted-foreground mb-2 px-1 text-xs font-medium uppercase tracking-wide">历史会话</p>
          {sessions.map(item => (
            <button key={item.id} type="button" onClick={() => navigate(`/agents/${agentId}/chat/${item.id}`)}
              className={cn('mb-1 flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-sm', item.id === sessionId ? 'bg-sidebar-accent font-medium' : 'text-muted-foreground hover:bg-sidebar-accent/60')}>
              <HistoryOutlined /><span className="truncate">{item.title || item.id.slice(0, 12)}</span>
            </button>
          ))}
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-between border-b px-5">
          <div className="flex items-center gap-2">
            <span className="font-serif text-lg font-medium">{agent?.name}</span>
            <Button.Group>
              <Button type={mode === 'chat' ? 'primary' : 'default'} onClick={() => setMode('chat')}>Chat</Button>
              <Button type={mode === 'plan' ? 'primary' : 'default'} onClick={() => setMode('plan')}>Plan</Button>
              {agent?.group_enabled && <Button type={mode === 'group' ? 'primary' : 'default'} onClick={() => setMode('group')}>Group</Button>}
            </Button.Group>
          </div>
          <Space>
            {sessionId && <Tag>{`${mode === 'plan' ? 'Plan Owner' : mode === 'group' ? 'Group' : 'Chat'} · ${sessionId.slice(0, 8)}`}</Tag>}
            {sessionRecord && <Tag color={sessionRecord.permission_mode === 'plan_readonly' ? 'gold' : 'green'}>
              {sessionRecord.permission_mode} · r{sessionRecord.permission_revision}
            </Tag>}
            {activeRun?.execution_kind === 'draft_test' && <Tag color="purple">草稿测试快照</Tag>}
            {isRunning && <Tag color="processing">后台执行中</Tag>}
            {isRunning && <Button danger size="small" icon={<StopOutlined />} loading={cancel.isPending} onClick={() => Modal.confirm({
              title: '取消本次 Run？', content: '取消会阻止后续执行，但不会回滚已经完成的 Tool 或外部副作用。',
              okText: '确认取消', okButtonProps: { danger: true }, cancelText: '继续执行', onOk: () => cancel.mutateAsync(),
            })}>取消 Run</Button>}
            {mode === 'group' && groupIsOpen && <Button danger size="small" loading={closeGroup.isPending}
              onClick={() => Modal.confirm({
                title: '关闭整个 Group？',
                content: '所有非终态 GroupRun 会被取消，Group 之后只允许读取历史；此操作不同于仅取消当前 Run。',
                okText: '关闭 Group', okButtonProps: { danger: true }, onOk: () => closeGroup.mutateAsync(),
              })}>关闭 Group</Button>}
          </Space>
        </header>
        {!connected && <Alert banner type="warning" message="实时事件连接中断，任务仍在后台执行；正在自动重连。" />}

        <section className="flex-1 overflow-y-auto px-5 py-6">
          <div className="mx-auto max-w-3xl">
            {toolActions.map(action => (
              <Card key={action.id} size="small" title={<Space><Tag color="warning">需要确认</Tag><span>{action.tool_key}</span></Space>} className="mb-4 border-amber-300">
                <p className="mb-2 text-sm text-muted-foreground">Agent 请求执行以下精确参数。批准只对本次参数哈希有效。</p>
                <pre className="mb-3 max-h-48 overflow-auto rounded bg-muted p-3 text-xs">{JSON.stringify(action.arguments, null, 2)}</pre>
                <Space>
                  <Button type="primary" loading={decide.isPending} onClick={() => decide.mutate({ action, decision: 'approve' })}>批准</Button>
                  <Button danger loading={decide.isPending} onClick={() => decide.mutate({ action, decision: 'reject' })}>拒绝</Button>
                </Space>
              </Card>
            ))}
            {clarificationActions.map(action => (
              <Card key={action.id} size="small" title={<Space><Tag color="blue">需要补充信息</Tag><span>Agent 提问</span></Space>} className="mb-4 border-blue-300">
                <p className="mb-3 whitespace-pre-wrap text-sm">{action.question}</p>
                <Input.TextArea value={clarificationAnswers[action.id] || ''}
                  onChange={event => setClarificationAnswers(value => ({ ...value, [action.id]: event.target.value }))}
                  autoSize={{ minRows: 2, maxRows: 6 }} placeholder="输入回答；只会恢复提出该问题的 Work Item" />
                <Button className="mt-3" type="primary" loading={answerClarification.isPending}
                  disabled={!clarificationAnswers[action.id]?.trim()}
                  onClick={() => answerClarification.mutate({ action, answer: clarificationAnswers[action.id].trim() })}>
                  提交回答
                </Button>
              </Card>
            ))}
            {mode === 'chat' && activeWorkingPlan && (
              <Card size="small" title={<Space><Tag color="purple">WorkingPlan</Tag><span>revision {activeWorkingPlan.revision}</span></Space>} className="mb-4 border-purple-200">
                <p className="mb-2 text-sm text-muted-foreground">{activeWorkingPlan.summary}</p>
                <div className="space-y-1">
                  {activeWorkingPlan.items.map((item, index) => (
                    <div key={`${index}-${item.text}`} className="flex gap-2 text-sm">
                      <Tag>{item.status}</Tag><span>{item.text}</span>
                    </div>
                  ))}
                </div>
              </Card>
            )}
            {mode === 'plan' && <PlanPanel session={sessionRecord} run={activePlanRun}
              plan={activeWorkingPlan} collaborators={agentInvocations}
              approval={planApproval} artifacts={artifacts} busy={planBusy}
              onContinue={async value => { await revisePlan.mutateAsync(value) }}
              onApprove={async () => { await confirmPlan.mutateAsync() }}
              onReject={async reason => { await rejectPlan.mutateAsync(reason) }}
              onExit={async () => { await exitPlan.mutateAsync() }}
              onDownload={sessionsApi.downloadArtifact} />}
            {mode === 'group' && <GroupPanel group={activeGroup} members={groupMembers} runs={groupRuns}
              messages={groupMessages} graph={interactionGraph} cards={networkCards?.items || []}
              plans={groupPlans} artifacts={artifacts} requiredActions={sessionActions}
              sharedTasks={sharedTasks} busy={groupBusy}
              onOpenNetwork={() => navigate('/network')}
              onReplaceReporter={async value => { await replaceReporter.mutateAsync(value) }}
              onDownload={sessionsApi.downloadArtifact} />}
            {mode === 'chat' && <>
            {historyLoading && <div className="py-20 text-center"><Spin /><p className="mt-3 text-sm text-muted-foreground">恢复持久消息…</p></div>}
            {!historyLoading && messages.length === 0 && (
              <div className="py-20 text-center">
                <div className="mb-3 text-3xl">✦</div>
                <p className="text-lg font-semibold">开始与 {agent?.name} 协作</p>
                <p className="mt-2 text-sm text-muted-foreground">提交后即使关闭页面，Run 仍会在后台可靠完成。</p>
              </div>
            )}
            {messages.filter(item => item.role === 'user' || item.role === 'assistant').map((item: Message) => (
              <div key={item.id} className={cn('mb-4 flex', item.role === 'user' ? 'justify-end' : 'justify-start')}>
                <div className={cn('max-w-[82%] whitespace-pre-wrap rounded-2xl px-4 py-2.5 text-sm leading-relaxed', item.role === 'user' ? 'rounded-br-md bg-primary text-primary-foreground' : 'rounded-bl-md bg-muted')}>
                  {item.content}
                </div>
              </div>
            ))}
            {isRunning && <div className="mb-4 flex justify-start"><div className="max-w-[82%] whitespace-pre-wrap rounded-2xl rounded-bl-md bg-muted px-4 py-3">{liveText || <><Spin size="small" /> <span className="ml-2 text-sm text-muted-foreground">Agent 正在后台处理…</span></>}</div></div>}
            </>}
            <div ref={bottomRef} />
          </div>
        </section>

        <footer className="border-t px-5 py-4">
          <div className="mx-auto flex max-w-3xl gap-2">
            {mode === 'group' && activeGroup && <Select
              className="w-56 shrink-0"
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
            />}
            <Input.TextArea value={input} onChange={event => setInput(event.target.value)} autoSize={{ minRows: 1, maxRows: 6 }}
              placeholder={mode === 'plan' ? (planAlreadyEntered ? '请在 Plan 面板中继续讨论、修改方向或批准' : '描述需要只读调查与规划的长任务目标') : mode === 'group' ? (activeGroup ? '直接 @所选成员，或开始后续 GroupRun' : '请先从 Agent Network 选择三名成员') : '输入消息；Enter 发送，Shift+Enter 换行'}
              disabled={inputDisabled}
              onPressEnter={event => { if (!event.shiftKey) { event.preventDefault(); if (input.trim()) (mode === 'plan' ? sendPlan : mode === 'group' ? sendGroup : send).mutate(input.trim()) } }} />
            <Button type="primary" icon={<SendOutlined />} loading={send.isPending || sendPlan.isPending || sendGroup.isPending}
              disabled={!input.trim() || inputDisabled}
              onClick={() => (mode === 'plan' ? sendPlan : mode === 'group' ? sendGroup : send).mutate(input.trim())}>{mode === 'plan' ? '进入只读 Plan' : mode === 'group' ? '提交 Group 工作' : '发送'}</Button>
          </div>
        </footer>
      </main>
      <SessionInspector sessionId={sessionId} />
    </div>
  )
}
