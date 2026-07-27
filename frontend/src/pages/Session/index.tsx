import { useState, useRef, useEffect, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Input, Button, Space, Typography, Tag, Spin, Divider, message, Steps, Card } from 'antd'
import {
  SendOutlined, ArrowLeftOutlined, HistoryOutlined, BranchesOutlined,
  CheckOutlined, EditOutlined, ThunderboltOutlined, SaveOutlined,
} from '@ant-design/icons'
import { sessionsApi, chatStream, type PlanPreview, type PlanStep } from '@/api/sessions'
import { agentsApi } from '@/api/agents'
import { WS_BASE } from '@/api/client'
import { cn } from '@/lib/utils'

interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

const PLAN_STATUS: Record<string, 'wait' | 'process' | 'finish' | 'error'> = {
  pending: 'wait', running: 'process', completed: 'finish', failed: 'error',
}

export default function SessionPage() {
  const { agentId } = useParams<{ agentId: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [plan, setPlan] = useState<PlanPreview | null>(null)
  const [planLoading, setPlanLoading] = useState(false)
  const [uiMode, setUiMode] = useState<'chat' | 'plan'>('chat')
  const [reviseMode, setReviseMode] = useState(false)
  const [executing, setExecuting] = useState(false)
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const wsRef = useRef<WebSocket | null>(null)

  const { data: agent } = useQuery({
    queryKey: ['agent', agentId],
    queryFn: () => agentsApi.get(agentId!),
    enabled: !!agentId,
  })

  const { data: sessions } = useQuery({
    queryKey: ['sessions', agentId],
    queryFn: () => sessionsApi.list(agentId),
    enabled: !!agentId,
  })

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const handleWsMessage = useCallback((evt: MessageEvent) => {
    try {
      const payload = JSON.parse(evt.data)
      const eventType = payload.type
      const data = payload.data ?? payload

      if (eventType === 'task_output' && data.content) {
        setMessages(prev => [...prev, { role: 'assistant', content: data.content }])
      }

      if (eventType === 'plan_step_update' && data.steps) {
        setPlan(prev => prev ? {
          ...prev,
          steps: data.steps as PlanStep[],
          status: 'running',
        } : prev)
        setExecuting(true)
      }

      if (eventType === 'plan_completed') {
        setExecuting(false)
        if (data.steps) {
          setPlan(prev => prev ? {
            ...prev,
            steps: data.steps as PlanStep[],
            status: data.status === 'completed' ? 'completed' : 'failed',
          } : prev)
        }
        if (data.status === 'completed') {
          message.success('计划执行完成')
          if (data.summary) {
            setMessages(prev => [...prev, { role: 'assistant', content: data.summary }])
          }
        } else {
          message.error(data.error || '计划执行失败')
        }
      }
    } catch {
      // ignore malformed events
    }
  }, [])

  useEffect(() => {
    if (!sessionId) return
    const ws = new WebSocket(`${WS_BASE}/ws/sessions/${sessionId}`)
    wsRef.current = ws
    ws.onmessage = handleWsMessage
    return () => { ws.close(); wsRef.current = null }
  }, [sessionId, handleWsMessage])

  const ensureSession = async (mode: 'chat' | 'plan') => {
    if (sessionId) return sessionId
    const s = await sessionsApi.create(agentId!, mode)
    setSessionId(s.id)
    queryClient.invalidateQueries({ queryKey: ['sessions', agentId] })
    return s.id as string
  }

  const sendChat = async () => {
    if (!input.trim() || streaming) return
    const msg = input.trim()
    setInput('')

    if (reviseMode && plan && sessionId) {
      setPlanLoading(true)
      try {
        const revised = await sessionsApi.revisePlan(sessionId, plan.plan_id, msg)
        setPlan(revised)
        setReviseMode(false)
        setUiMode('plan')
        setMessages(prev => [
          ...prev,
          { role: 'user', content: `[计划修改] ${msg}` },
          { role: 'assistant', content: '已根据你的描述更新执行计划，请确认后执行。' },
        ])
      } catch {
        message.error('计划修改失败')
      } finally {
        setPlanLoading(false)
      }
      return
    }

    setMessages(prev => [...prev, { role: 'user', content: msg }, { role: 'assistant', content: '' }])
    setStreaming(true)
    const sid = await ensureSession('chat')
    chatStream(
      sid,
      msg,
      (chunk) => {
        setMessages(prev => {
          const copy = [...prev]
          copy[copy.length - 1] = { ...copy[copy.length - 1], content: copy[copy.length - 1].content + chunk }
          return copy
        })
      },
      () => setStreaming(false),
      (err) => {
        setMessages(prev => {
          const copy = [...prev]
          copy[copy.length - 1] = { ...copy[copy.length - 1], content: `[错误] ${err}` }
          return copy
        })
        setStreaming(false)
      },
    )
  }

  const generatePlan = async () => {
    if (!input.trim()) return
    const goal = input.trim()
    setInput('')
    setPlanLoading(true)
    try {
      const sid = await ensureSession('plan')
      const res = await sessionsApi.generatePlan(sid, goal)
      setPlan(res)
      setUiMode('plan')
      setReviseMode(false)
      setExecuting(false)
      setActiveTaskId(null)
    } catch {
      message.error('计划生成失败')
    } finally {
      setPlanLoading(false)
    }
  }

  const updateStepDescription = (stepId: string, description: string) => {
    if (!plan) return
    setPlan({
      ...plan,
      steps: plan.steps.map(s => s.id === stepId ? { ...s, description } : s),
    })
  }

  const savePlanEdits = useMutation({
    mutationFn: () => sessionsApi.updatePlan(sessionId!, plan!.plan_id, plan!.steps),
    onSuccess: (data) => {
      setPlan(data)
      message.success('计划已保存')
    },
    onError: () => message.error('保存失败'),
  })

  const executePlanMutation = useMutation({
    mutationFn: async () => {
      if (sessionId && plan) {
        const saved = await sessionsApi.updatePlan(sessionId, plan.plan_id, plan.steps)
        setPlan(saved)
        return sessionsApi.executePlan(sessionId, plan.plan_id)
      }
      throw new Error('No session or plan')
    },
    onSuccess: (data) => {
      setExecuting(true)
      setActiveTaskId(data.task_id)
      setPlan(prev => prev ? { ...prev, status: 'running' } : prev)
      message.success('计划已开始执行')
    },
    onError: () => message.error('执行失败'),
  })

  const supportsPlan = agent?.modes?.includes('plan')
  const planEditable = plan && ['draft', 'confirmed'].includes(plan.status) && !executing

  return (
    <div className="bg-background flex h-full min-h-[calc(100vh-0px)]">
      <aside className="bg-sidebar flex w-56 shrink-0 flex-col border-r border-sidebar-border">
        <div className="space-y-2 border-b border-sidebar-border p-3">
          <Button icon={<ArrowLeftOutlined />} block onClick={() => navigate('/agents')}>
            返回 Agents
          </Button>
          <Button type="primary" block onClick={() => {
            setMessages([]); setSessionId(null); setPlan(null); setUiMode('chat')
            setReviseMode(false); setExecuting(false); setActiveTaskId(null)
          }}>
            新对话
          </Button>
          {sessionId && (
            <Button block icon={<BranchesOutlined />} onClick={() => navigate(`/sessions/${sessionId}/orchestration`)}>
              Group 编排
            </Button>
          )}
        </div>
        <div className="flex-1 overflow-y-auto p-3">
          <p className="text-muted-foreground mb-2 px-1 text-xs font-medium uppercase tracking-wide">
            历史会话
          </p>
          <div className="space-y-1">
            {(sessions || []).slice(0, 20).map((s: any) => (
              <button
                key={s.id}
                type="button"
                className={cn(
                  'flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm transition-colors',
                  s.id === sessionId
                    ? 'bg-sidebar-accent text-sidebar-accent-foreground font-medium'
                    : 'text-muted-foreground hover:bg-sidebar-accent/60',
                )}
                onClick={() => navigate(`/sessions/${s.id}/trace`)}
              >
                <HistoryOutlined className="shrink-0 text-xs" />
                <span className="truncate">{s.title || s.id.slice(0, 12)}</span>
              </button>
            ))}
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between border-b bg-background/80 px-5 backdrop-blur-sm">
          <div className="flex items-center gap-2">
            <span className="font-serif text-lg font-medium">{agent?.name}</span>
            {agent?.modes?.map((m: string) => (
              <Tag key={m} className="rounded-md border-0 bg-secondary text-xs">{m}</Tag>
            ))}
          </div>
          <Space>
            {sessionId && (
              <Button size="small" icon={<BranchesOutlined />} onClick={() => navigate(`/sessions/${sessionId}/trace`)}>
                查看 Trace
              </Button>
            )}
            {activeTaskId && (
              <Button size="small" onClick={() => navigate('/tasks')}>
                Task 看板
              </Button>
            )}
            <Tag color={uiMode === 'plan' ? 'purple' : 'default'}>{uiMode} 模式</Tag>
          </Space>
        </header>

        {uiMode === 'plan' && plan && (
          <Card
            size="small"
            className="mx-5 mt-4 rounded-xl"
            style={{ borderColor: '#d8b4fe' }}
            title={
              <Space>
                <ThunderboltOutlined style={{ color: '#9333ea' }} />
                <span>执行计划：{plan.goal}</span>
                <Tag color={plan.status === 'completed' ? 'success' : plan.status === 'failed' ? 'error' : 'processing'}>
                  {plan.status}
                </Tag>
              </Space>
            }
            extra={
              planEditable ? (
                <Space>
                  <Button
                    size="small"
                    icon={<EditOutlined />}
                    onClick={() => {
                      setReviseMode(true)
                      setUiMode('chat')
                      message.info('在下方输入自然语言描述如何修改计划')
                    }}
                  >
                    对话修改
                  </Button>
                  <Button
                    size="small"
                    icon={<SaveOutlined />}
                    onClick={() => savePlanEdits.mutate()}
                    loading={savePlanEdits.isPending}
                  >
                    保存编辑
                  </Button>
                  <Button
                    size="small"
                    type="primary"
                    icon={<CheckOutlined />}
                    onClick={() => executePlanMutation.mutate()}
                    loading={executePlanMutation.isPending}
                  >
                    确认执行
                  </Button>
                </Space>
              ) : executing ? (
                <Space><Spin size="small" /><span className="text-sm">执行中...</span></Space>
              ) : null
            }
          >
            <Steps
              direction="vertical"
              size="small"
              items={plan.steps.map(s => ({
                title: planEditable ? (
                  <Input.TextArea
                    value={s.description}
                    autoSize={{ minRows: 1, maxRows: 4 }}
                    onChange={e => updateStepDescription(s.id, e.target.value)}
                    className="text-sm"
                  />
                ) : s.description,
                status: PLAN_STATUS[s.status] || 'wait',
                description: (
                  <div className="text-xs">
                    {s.depends_on?.length ? `依赖: ${s.depends_on.join(', ')}` : null}
                    {s.output && (
                      <div className="text-muted-foreground mt-1 max-h-24 overflow-y-auto whitespace-pre-wrap">
                        {s.output.slice(0, 300)}{s.output.length > 300 ? '...' : ''}
                      </div>
                    )}
                  </div>
                ),
              }))}
            />
          </Card>
        )}

        <div className="flex-1 overflow-y-auto px-5 py-6">
          <div className="mx-auto max-w-3xl">
            {messages.length === 0 && !plan && (
              <div className="flex flex-col items-center justify-center py-20 text-center">
                <div className="mb-3 text-3xl">👋</div>
                <p className="text-lg font-semibold">开始与 {agent?.name} 对话</p>
                <p className="text-muted-foreground mt-2 max-w-sm text-sm">
                  {supportsPlan
                    ? '输入后按 Enter 发送（Chat），或点击「生成计划」进入 Plan 模式'
                    : '发消息开始对话'}
                </p>
              </div>
            )}
            {messages.map((msg, i) => (
              <div
                key={i}
                className={cn(
                  'mb-4 flex',
                  msg.role === 'user' ? 'justify-end' : 'justify-start',
                )}
              >
                <div
                  className={cn(
                    'max-w-[80%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap break-words',
                    msg.role === 'user'
                      ? 'bg-primary text-primary-foreground rounded-br-md'
                      : 'bg-muted text-foreground rounded-bl-md',
                  )}
                >
                  {msg.content || (streaming && i === messages.length - 1 ? <Spin size="small" /> : '')}
                </div>
              </div>
            ))}
            <div ref={bottomRef} />
          </div>
        </div>

        <div className="border-t bg-background px-5 py-4">
          <div className="mx-auto max-w-3xl">
            {reviseMode && (
              <div className="mb-2 rounded-lg bg-purple-50 px-3 py-2 text-sm text-purple-800">
                对话修改模式：描述你想如何调整计划，Enter 发送
              </div>
            )}
            {planLoading && (
              <div className="text-muted-foreground mb-2 flex items-center gap-2 text-sm">
                <Spin size="small" />
                AI 正在{reviseMode ? '修改' : '生成'}执行计划...
              </div>
            )}
            <div className="flex gap-2">
              <Input
                value={input}
                onChange={e => setInput(e.target.value)}
                onPressEnter={e => { if (!e.shiftKey) sendChat() }}
                placeholder={
                  reviseMode
                    ? '描述如何修改计划，Enter 发送'
                    : supportsPlan
                      ? '输入目标描述，Enter=发送，或点「生成计划」'
                      : '输入消息，Enter 发送'
                }
                disabled={streaming || planLoading || executing}
                size="large"
                className="rounded-xl"
              />
              {supportsPlan && !reviseMode && (
                <Button
                  onClick={generatePlan}
                  loading={planLoading}
                  disabled={executing}
                  size="large"
                  className="rounded-xl"
                >
                  生成计划
                </Button>
              )}
              <Button
                type="primary"
                icon={<SendOutlined />}
                onClick={sendChat}
                loading={streaming || planLoading}
                disabled={executing}
                size="large"
                className="rounded-xl"
              >
                发送
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
