import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Alert, Button, Card, Checkbox, Empty, Input, Select, Space, Tag, Typography, message } from 'antd'
import { Network, Search, Users } from 'lucide-react'

import { AgentCard, agentsApi } from '@/api/agents'
import { sessionsApi } from '@/api/sessions'

const { Title, Paragraph, Text } = Typography

export default function AgentNetworkPage() {
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [submittedQuery, setSubmittedQuery] = useState('')
  const [directId, setDirectId] = useState('')
  const [directCard, setDirectCard] = useState<AgentCard | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [reporter, setReporter] = useState<string>()
  const [goal, setGoal] = useState('')

  const { data, isLoading } = useQuery({
    queryKey: ['agent-network', submittedQuery],
    queryFn: () => agentsApi.searchNetwork({
      query: submittedQuery || undefined,
      group_enabled: true,
      limit: 100,
    }),
  })
  const cards = useMemo(() => {
    const rows = [...(data?.items || [])]
    if (directCard && !rows.some(item => item.agent_id === directCard.agent_id)) rows.unshift(directCard)
    return rows
  }, [data?.items, directCard])
  const selectedCards = selected.map(id => cards.find(card => card.agent_id === id)).filter(Boolean) as AgentCard[]

  const resolve = useMutation({
    mutationFn: () => agentsApi.resolveNetworkAgent(directId.trim()),
    onSuccess: card => {
      if (!card.group_enabled) return message.error('该 Agent 未启用 Group 协作')
      setDirectCard(card)
      message.success('已通过 Agent ID 解析当前发布版本')
    },
    onError: () => message.error('Agent ID 不可见、未发布或不满足治理条件'),
  })
  const create = useMutation({
    mutationFn: async () => {
      if (selected.length !== 3) throw new Error('第一期 Group 必须选择恰好三名成员')
      if (!goal.trim()) throw new Error('请输入 Group 目标')
      const reporterId = reporter || selected[0]
      const session = await sessionsApi.create(reporterId)
      const group = await sessionsApi.createGroup(session.id, {
        member_agent_ids: selected,
        reporter_agent_id: reporterId,
        goal: goal.trim(),
        shared_context_refs: [],
      })
      return { session, group, reporterId }
    },
    onSuccess: ({ session, group, reporterId }) => {
      navigate(`/agents/${reporterId}/chat/${session.id}?mode=group&group=${group.id}`)
    },
    onError: (error: any) => message.error(
      error?.response?.data?.detail?.message || error?.message || 'Group 创建失败',
    ),
  })

  const toggle = (agentId: string, checked: boolean) => {
    setSelected(current => {
      const next = checked
        ? [...current.filter(id => id !== agentId), agentId]
        : current.filter(id => id !== agentId)
      if (next.length > 3) {
        message.warning('第一期 Group 固定由用户选择三名成员')
        return current
      }
      if (!next.includes(reporter || '')) setReporter(next[0])
      return next
    })
  }

  return (
    <div className="mx-auto max-w-6xl space-y-5">
      <div>
        <Title level={2} className="!mb-1"><Space><Network className="size-6" />Agent Network</Space></Title>
        <Paragraph type="secondary">
          这里只展示当前已发布、可见、可用且通过治理过滤的 AgentCard。选择三名对等成员后直接建组，不生成 Formation 或固定流程。
        </Paragraph>
      </div>

      <Card>
        <div className="grid gap-3 md:grid-cols-2">
          <Input.Search
            value={query}
            onChange={event => setQuery(event.target.value)}
            onSearch={value => setSubmittedQuery(value.trim())}
            enterButton={<><Search className="mr-1 inline size-4" />搜索工作网</>}
            placeholder="名称、领域、支持结果"
          />
          <Space.Compact block>
            <Input value={directId} onChange={event => setDirectId(event.target.value)} placeholder="直接输入 Agent ID" />
            <Button loading={resolve.isPending} disabled={!directId.trim()} onClick={() => resolve.mutate()}>解析 ID</Button>
          </Space.Compact>
        </div>
      </Card>

      {cards.length === 0 && !isLoading ? <Empty description="没有满足当前发布与治理条件的 Group Agent" /> : (
        <div className="grid gap-3 lg:grid-cols-3">
          {cards.map(card => {
            const checked = selected.includes(card.agent_id)
            const capabilities = (card.capabilities_summary.tool_keys || card.capabilities_summary.capabilities || []) as string[]
            return (
              <Card key={card.agent_id} className={checked ? 'border-primary shadow-sm' : ''}
                title={<Checkbox checked={checked} onChange={event => toggle(card.agent_id, event.target.checked)}>{card.name}</Checkbox>}
                extra={<Tag color="green">published</Tag>}>
                <Paragraph className="min-h-11" ellipsis={{ rows: 2 }}>{card.description}</Paragraph>
                <div className="mb-3 flex flex-wrap gap-1">
                  {card.domain_tags.map(tag => <Tag key={tag}>{tag}</Tag>)}
                  {capabilities.slice(0, 3).map(item => <Tag color="blue" key={item}>{item}</Tag>)}
                </div>
                <Text type="secondary" className="block text-xs">Agent {card.agent_id}</Text>
                <Text type="secondary" className="block text-xs">Version {card.agent_version_id}</Text>
              </Card>
            )
          })}
        </div>
      )}

      <Card title={<Space><Users className="size-4" />创建第一期 Group</Space>}>
        <Alert className="mb-4" type={selected.length === 3 ? 'success' : 'info'} showIcon
          message={`已选择 ${selected.length}/3 名成员`}
          description={selectedCards.map(card => card.name).join(' · ') || '从上方工作网选择产品、开发、测试 Agent。角色由各自发布声明表达，不会被 Group 硬编码赋权。'} />
        <div className="grid gap-3 md:grid-cols-[1fr_280px]">
          <Input.TextArea value={goal} onChange={event => setGoal(event.target.value)} autoSize={{ minRows: 3, maxRows: 8 }}
            placeholder="描述真实协作目标，例如：实现容量风险计算器，产出程序、自动化测试与可下载 Artifact。" />
          <div className="space-y-3">
            <Select className="w-full" value={reporter} onChange={setReporter} placeholder="选择本次 Reporter"
              options={selectedCards.map(card => ({ value: card.agent_id, label: card.name }))} />
            <Button type="primary" block size="large" loading={create.isPending}
              disabled={selected.length !== 3 || !goal.trim()} onClick={() => create.mutate()}>
              直接建组并开始 GroupRun
            </Button>
            <Text type="secondary" className="block text-xs">Reporter 只负责本次自然收口，可在不可用时由用户替换，不是永久 Supervisor。</Text>
          </div>
        </div>
      </Card>
    </div>
  )
}
