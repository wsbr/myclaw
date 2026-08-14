import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Empty, Input, Space, Tag, Typography, message } from 'antd'
import { Network, Search } from 'lucide-react'

import { AgentCard, agentsApi } from '@/api/agents'

const { Title, Paragraph, Text } = Typography

export default function AgentNetworkPage() {
  const [query, setQuery] = useState('')
  const [submittedQuery, setSubmittedQuery] = useState('')
  const [directId, setDirectId] = useState('')
  const [directCard, setDirectCard] = useState<AgentCard | null>(null)

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

  const resolve = useMutation({
    mutationFn: () => agentsApi.resolveNetworkAgent(directId.trim()),
    onSuccess: card => {
      if (!card.group_enabled) return message.error('该 Agent 未启用 Group 协作')
      setDirectCard(card)
      message.success('已通过 Agent ID 解析当前发布版本')
    },
    onError: () => message.error('Agent ID 不可见、未发布或不满足治理条件'),
  })
  return (
    <div className="mx-auto max-w-6xl space-y-5">
      <div>
        <Title level={2} className="!mb-1"><Space><Network className="size-6" />Agent Network</Space></Title>
        <Paragraph type="secondary">
          这里只展示当前已发布、可见、可用且通过治理过滤的 Group Agent。它是只读候选目录；正常 Chat 中，模型会根据用户诉求读取 Agent 的名称与描述，选择必要成员，再在 Chat 完成后涌现为 Group。
        </Paragraph>
      </div>

      <Alert type="info" showIcon
        message="Group 不再手动选三人"
        description="每次正常 Chat 完成后，系统按当时的发布版本、可见性、可调用性和 Group 治理规则纳入全部已开启 Group 的 Agent；每个 GroupRun 会冻结自己的成员与版本快照。" />

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
            const capabilities = (card.capabilities_summary.tool_keys || card.capabilities_summary.capabilities || []) as string[]
            return (
              <Card key={card.agent_id}
                title={card.name}
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
    </div>
  )
}
