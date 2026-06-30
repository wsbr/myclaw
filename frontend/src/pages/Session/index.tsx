import { useState, useRef, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Layout, Input, Button, Space, Typography, Tag, Spin, Divider, message,
  Steps, Card,
} from 'antd'
import {
  SendOutlined, ArrowLeftOutlined, HistoryOutlined, BranchesOutlined,
  CheckOutlined, EditOutlined, ThunderboltOutlined,
} from '@ant-design/icons'
import { sessionsApi, chatStream } from '../../api/sessions'
import { agentsApi } from '../../api/agents'
import client, { WS_BASE } from '../../api/client'

const { Sider, Content } = Layout

interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

interface PlanStep {
  id: string
  description: string
  status: 'pending' | 'running' | 'completed' | 'failed'
  depends_on?: string[]
}

interface Plan {
  plan_id: string
  goal: string
  steps: PlanStep[]
}

const PLAN_STATUS: Record<string, any> = {
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
  const [plan, setPlan] = useState<Plan | null>(null)
  const [planLoading, setPlanLoading] = useState(false)
  const [uiMode, setUiMode] = useState<'chat' | 'plan'>('chat')
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

  // WebSocket — 实时接收 task_output 和 span 事件
  useEffect(() => {
    if (!sessionId) return
    const ws = new WebSocket(`${WS_BASE}/ws/sessions/${sessionId}`)
    wsRef.current = ws
    ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data)
        if (data.type === 'task_output') {
          setMessages(prev => [...prev, { role: 'assistant', content: data.content }])
        }
      } catch {}
    }
    return () => { ws.close(); wsRef.current = null }
  }, [sessionId])

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
      const res = await client.post(`/sessions/${sid}/plan/generate`, { goal })
      setPlan(res.data)
      setUiMode('plan')
    } catch {
      message.error('计划生成失败')
    } finally {
      setPlanLoading(false)
    }
  }

  const executePlanMutation = useMutation({
    mutationFn: () => client.post(`/sessions/${sessionId}/plan/${plan!.plan_id}/execute`),
    onSuccess: () => {
      message.success('计划已提交，可在 Task 看板查看进度')
      navigate('/tasks')
    },
    onError: () => message.error('执行失败'),
  })

  const supportsPlan = agent?.modes?.includes('plan')

  return (
    <Layout style={{ height: '100%' }}>
      <Sider theme="light" width={240} style={{ borderRight: '1px solid #f0f0f0', padding: 12, overflowY: 'auto' }}>
        <Space direction="vertical" style={{ width: '100%' }}>
          <Button icon={<ArrowLeftOutlined />} block onClick={() => navigate('/agents')}>返回</Button>
          <Button type="primary" block onClick={() => {
            setMessages([]); setSessionId(null); setPlan(null); setUiMode('chat')
          }}>新对话</Button>
          {sessionId && (
            <Button block icon={<BranchesOutlined />} onClick={() => navigate(`/sessions/${sessionId}/orchestration`)}>
              Group 编排
            </Button>
          )}
          <Divider style={{ margin: '8px 0' }} />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>历史会话</Typography.Text>
          {(sessions || []).slice(0, 20).map((s: any) => (
            <Button
              key={s.id}
              block
              type={s.id === sessionId ? 'primary' : 'text'}
              style={{ textAlign: 'left', overflow: 'hidden', textOverflow: 'ellipsis' }}
              onClick={() => navigate(`/sessions/${s.id}/trace`)}
            >
              <HistoryOutlined /> {s.title || s.id.slice(0, 12)}
            </Button>
          ))}
        </Space>
      </Sider>

      <Content style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
        {/* 顶部 */}
        <div style={{ padding: '12px 20px', borderBottom: '1px solid #f0f0f0', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Space>
            <Typography.Title level={5} style={{ margin: 0 }}>{agent?.name}</Typography.Title>
            {agent?.modes?.map((m: string) => <Tag key={m}>{m}</Tag>)}
          </Space>
          <Space>
            {sessionId && (
              <Button size="small" icon={<BranchesOutlined />} onClick={() => navigate(`/sessions/${sessionId}/trace`)}>
                查看 Trace
              </Button>
            )}
            <Tag color={uiMode === 'plan' ? 'purple' : 'blue'}>{uiMode} 模式</Tag>
          </Space>
        </div>

        {/* Plan 预览区 */}
        {uiMode === 'plan' && plan && (
          <Card
            size="small"
            style={{ margin: '12px 20px', borderColor: '#722ed1' }}
            title={
              <Space>
                <ThunderboltOutlined style={{ color: '#722ed1' }} />
                <span>执行计划：{plan.goal}</span>
              </Space>
            }
            extra={
              <Space>
                <Button size="small" icon={<EditOutlined />} onClick={() => setUiMode('chat')}>
                  对话修改
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
            }
          >
            <Steps
              direction="vertical"
              size="small"
              items={plan.steps.map(s => ({
                title: s.description,
                status: PLAN_STATUS[s.status] || 'wait',
                description: s.depends_on?.length ? `依赖步骤: ${s.depends_on.join(', ')}` : undefined,
              }))}
            />
          </Card>
        )}

        {/* 消息列表 */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '16px 20px' }}>
          {messages.length === 0 && !plan && (
            <div style={{ textAlign: 'center', marginTop: 80 }}>
              <Typography.Text type="secondary">
                {supportsPlan
                  ? '输入后按 Enter 发送（Chat），或点击"生成计划"进入 Plan 模式'
                  : '发消息开始对话'}
              </Typography.Text>
            </div>
          )}
          {messages.map((msg, i) => (
            <div
              key={i}
              style={{
                marginBottom: 16,
                display: 'flex',
                flexDirection: msg.role === 'user' ? 'row-reverse' : 'row',
                gap: 8,
              }}
            >
              <div style={{
                maxWidth: '72%',
                padding: '10px 14px',
                borderRadius: 12,
                background: msg.role === 'user' ? '#1677ff' : '#f5f5f5',
                color: msg.role === 'user' ? '#fff' : '#000',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}>
                {msg.content || (streaming && i === messages.length - 1 ? <Spin size="small" /> : '')}
              </div>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>

        {/* 输入框 */}
        <div style={{ padding: '12px 20px', borderTop: '1px solid #f0f0f0' }}>
          {planLoading && (
            <div style={{ marginBottom: 8 }}>
              <Spin size="small" /> <Typography.Text type="secondary">AI 正在生成执行计划...</Typography.Text>
            </div>
          )}
          <Space.Compact style={{ width: '100%' }}>
            <Input
              value={input}
              onChange={e => setInput(e.target.value)}
              onPressEnter={e => { if (!e.shiftKey) sendChat() }}
              placeholder={supportsPlan ? '输入目标描述，Enter=发送，或点"生成计划"' : '输入消息，Enter 发送'}
              disabled={streaming || planLoading}
              size="large"
            />
            {supportsPlan && (
              <Button
                onClick={generatePlan}
                loading={planLoading}
                size="large"
                style={{ borderColor: '#722ed1', color: '#722ed1' }}
              >
                生成计划
              </Button>
            )}
            <Button
              type="primary"
              icon={<SendOutlined />}
              onClick={sendChat}
              loading={streaming}
              size="large"
            >
              发送
            </Button>
          </Space.Compact>
        </div>
      </Content>
    </Layout>
  )
}
