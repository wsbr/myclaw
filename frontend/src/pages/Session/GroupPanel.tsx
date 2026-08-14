import { Alert, Button, Card, Descriptions, Empty, Select, Space, Table, Tag, Typography } from 'antd'
import ReactFlow, { Background, Controls, MarkerType, type Edge, type Node } from 'reactflow'
import 'reactflow/dist/style.css'

import type { AgentCard } from '@/api/agents'
import type {
  Artifact,
  GroupMembership,
  GroupMessage,
  GroupRun,
  GroupSession,
  InteractionGraph,
  RequiredAction,
  SharedTask,
  WorkingPlan,
} from '@/api/sessions'

interface Props {
  group?: GroupSession
  activeRun?: GroupRun
  cards: AgentCard[]
  members: GroupMembership[]
  runs: GroupRun[]
  messages: GroupMessage[]
  graph?: InteractionGraph
  plans: WorkingPlan[]
  artifacts: Artifact[]
  requiredActions: RequiredAction[]
  sharedTasks: SharedTask[]
  busy: boolean
  onSelectRun: (groupRunId: string) => void
  onReplaceReporter: (agentId: string) => Promise<void>
  onDownload: (artifact: Artifact) => void
}

const short = (value?: string | null) => value ? value.slice(0, 10) : '—'

export default function GroupPanel({
  group, activeRun, cards, members, runs, messages, graph, plans, artifacts,
  requiredActions, sharedTasks, busy, onSelectRun, onReplaceReporter, onDownload,
}: Props) {
  const cardByAgent = new Map(cards.map(card => [card.agent_id, card]))
  const name = (agentId?: string | null) => agentId ? cardByAgent.get(agentId)?.name || short(agentId) : 'Group'
  const availableMembers = activeRun?.members.filter(item => item.participation_status === 'available') || []

  if (!group) {
    return (
      <Card className="mb-5 text-center">
        <Empty description="当前 Session 尚未建立 Agent-native Group">
          <Typography.Paragraph type="secondary">
            先在 Chat 中正常对话。模型会根据本次诉求，从当前可用、治理允许且已开启 Group 的 Agent 目录中选择必要成员，Chat 完成后再涌现协作。
          </Typography.Paragraph>
        </Empty>
      </Card>
    )
  }

  const graphMembers = [...(graph?.nodes || activeRun?.members || [])].sort((left, right) => {
    if (left.agent_id === activeRun?.reporter_agent_id) return -1
    if (right.agent_id === activeRun?.reporter_agent_id) return 1
    return left.agent_id.localeCompare(right.agent_id)
  })
  const hasGroupBroadcast = Boolean(graph?.edges.some(edge => edge.cause_type === 'group_message'))
  const graphNodes: Node[] = graphMembers.map((member, index) => {
    const peerIndex = Math.max(0, index - 1)
    const ring = Math.floor(peerIndex / 8) + 1
    const ringCount = Math.min(8, Math.max(1, graphMembers.length - 1 - (ring - 1) * 8))
    const angle = (2 * Math.PI * (peerIndex % 8)) / ringCount
    const radius = 290 * ring
    return {
      id: member.agent_id,
      position: index === 0
        ? { x: 0, y: 0 }
        : { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius },
      data: {
        label: (
          <div className="min-w-40">
            <b>{name(member.agent_id)}</b>
            <div className="mt-1 text-xs">{member.participation_status} · v{short(member.agent_version_id)}</div>
            {activeRun?.reporter_agent_id === member.agent_id && <Tag className="mt-1" color="purple">Reporter</Tag>}
          </div>
        ),
      },
      style: {
        borderColor: member.participation_status === 'available' ? '#22c55e' : '#ef4444',
        borderRadius: 10,
      },
    }
  })
  if (hasGroupBroadcast && activeRun) graphNodes.push({
    id: `group:${activeRun.id}`,
    position: { x: 210, y: -150 },
    data: { label: <b>Group 留痕</b> },
    style: { borderColor: '#7c3aed', borderRadius: 999, background: '#f5f3ff' },
  })
  const edgeBuckets = new Map<string, NonNullable<typeof graph>['edges']>()
  for (const edge of graph?.edges || []) {
    const key = `${edge.source_agent_id}:${edge.target_node_id}:${edge.cause_type}`
    edgeBuckets.set(key, [...(edgeBuckets.get(key) || []), edge])
  }
  const graphEdges: Edge[] = [...edgeBuckets.entries()].map(([key, facts]) => {
    const fact = facts[0]
    const label = fact.cause_type === 'message'
      ? 'message'
      : fact.cause_type === 'group_message' ? 'record' : 'invoke_agent'
    return {
      id: key,
      source: fact.source_agent_id,
      target: fact.target_node_id,
      type: 'smoothstep',
      label: `${label}${facts.length > 1 ? ` ×${facts.length}` : ''}`,
      animated: facts.some(item => item.status === 'queued' || item.status === 'running'),
      markerEnd: { type: MarkerType.ArrowClosed },
      style: {
        stroke: fact.cause_type === 'message' ? '#0f766e' : fact.cause_type === 'group_message' ? '#7c3aed' : '#2563eb',
        strokeWidth: 2,
        strokeDasharray: fact.cause_type === 'group_message' ? '6 4' : undefined,
      },
    }
  })

  return (
    <div className="space-y-4">
      <Card size="small" title="Group 状态" extra={<Tag color={group.status === 'open' ? 'green' : 'default'}>{group.status}</Tag>}>
        <Descriptions size="small" column={2}>
          <Descriptions.Item label="Group">{short(group.id)}</Descriptions.Item>
          <Descriptions.Item label="成员策略">{group.membership_policy}</Descriptions.Item>
          <Descriptions.Item label="形成方式">{group.formation_mode === 'emergent_model_selected' ? 'Chat 模型语义选员' : '手动选择（历史）'}</Descriptions.Item>
          <Descriptions.Item label="Initiator">{name(group.initiator_agent_id)}</Descriptions.Item>
          <Descriptions.Item label="成员">{members.filter(item => item.status === 'joined').length}</Descriptions.Item>
          <Descriptions.Item label="目标" span={2}>{activeRun?.goal || '—'}</Descriptions.Item>
        </Descriptions>
        <Select
          className="mt-3 w-full"
          value={activeRun?.id}
          onChange={onSelectRun}
          placeholder="选择要审计的 GroupRun"
          options={runs.map(item => ({
            value: item.id,
            label: `${short(item.id)} · ${item.run.status}${item.run.terminal_reason === 'completed_with_errors' ? '（有错误）' : ''} · ${item.goal.slice(0, 48)}`,
          }))}
        />
      </Card>

      {activeRun?.run.terminal_reason === 'completed_with_errors' && <Alert
        type="warning" showIcon
        message="本 GroupRun 已完成，但存在失败事实"
        description="图、消息与摘要均限定在当前选中的 Run；失败不会再被旧 Run 内容覆盖。"
      />}

      <Card size="small" title={`固定的 GroupRun 成员快照 · ${short(activeRun?.id)}`}>
        <Table size="small" pagination={false} rowKey="membership_id" dataSource={activeRun?.members || []}
          columns={[
            { title: 'Agent', dataIndex: 'agent_id', render: (value: string) => name(value) },
            { title: 'AgentVersion', dataIndex: 'agent_version_id', render: short },
            { title: '参与', dataIndex: 'participation_status', render: (value: string) => <Tag>{value}</Tag> },
            { title: '权限 / 能力边界', dataIndex: 'capability_boundary', render: (value: object) => <Typography.Text code>{JSON.stringify(value)}</Typography.Text> },
            { title: '预算', dataIndex: 'budget_snapshot', render: (value: object) => <Typography.Text code>{JSON.stringify(value)}</Typography.Text> },
          ]} />
      </Card>

      {activeRun?.run.status === 'waiting_user' && (
        <Card size="small" className="border-amber-300" title={<Space><Tag color="warning">Required Action</Tag>Reporter 不可用</Space>}>
          <Space wrap>
            <Typography.Text>选择另一名当前快照内可用成员继续本次 summary epoch：</Typography.Text>
            <Select className="w-56" defaultValue={activeRun.reporter_agent_id || undefined}
              options={availableMembers.map(item => ({ value: item.agent_id, label: name(item.agent_id) }))}
              onChange={value => onReplaceReporter(value)} disabled={busy} />
          </Space>
        </Card>
      )}

      <Card size="small" title="真实 Interaction Graph" extra={<Tag>{graphEdges.length} facts</Tag>}>
        {graphNodes.length ? (
          <div className="h-80 rounded border">
            <ReactFlow nodes={graphNodes} edges={graphEdges} fitView nodesDraggable={false}>
              <Background /><Controls showInteractive={false} />
            </ReactFlow>
          </div>
        ) : <Empty description="还没有真实 Message 或 AgentInvocation 互动" />}
        <Typography.Paragraph type="secondary" className="!mb-0 !mt-2 text-xs">
          图只显示当前选中 GroupRun 的持久化事实。委托的定向请求与交付折叠在 invoke_agent 边中；群留痕不通知成员，指向 Group 节点且不与历史 Run 混合。
        </Typography.Paragraph>
      </Card>

      <Card size="small" title="Group 消息">
        {messages.length === 0 ? <Empty description="暂无消息" /> : (
          <div className="max-h-96 space-y-2 overflow-y-auto">
            {messages.map(item => (
              <div key={item.id} className="rounded-lg border p-3">
                <Space wrap>
                  <Tag color={item.sender_type === 'user' ? 'gold' : 'blue'}>{item.sender_type === 'user' ? '用户' : name(item.sender_agent_id)}</Tag>
                  <span>→</span>
                  <Tag>{item.visibility === 'direct' ? `@${name(item.recipient_agent_id)}` : item.visibility === 'group' ? '群留痕' : item.visibility}</Tag>
                  {item.sequence_no && <Typography.Text type="secondary">#{item.sequence_no}</Typography.Text>}
                </Space>
                <div className="mt-2 whitespace-pre-wrap text-sm">{item.content}</div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card size="small" title="Runs / Summary epochs">
          <Table size="small" pagination={false} rowKey="id" dataSource={runs}
            onRow={item => ({ onClick: () => onSelectRun(item.id), className: item.id === activeRun?.id ? 'bg-blue-50/60 cursor-pointer' : 'cursor-pointer' })}
            columns={[
              { title: 'Run', dataIndex: 'id', render: short },
              { title: '状态', render: (_: unknown, item: GroupRun) => <Tag color={item.run.terminal_reason === 'completed_with_errors' ? 'warning' : undefined}>{item.run.status}{item.run.terminal_reason === 'completed_with_errors' ? ' · 有错误' : ''}</Tag> },
              { title: 'Reporter', dataIndex: 'reporter_agent_id', render: name },
              { title: 'Epoch', render: (_: unknown, item: GroupRun) => `${item.delivered_summary_epoch}/${item.summary_epoch}` },
            ]} />
        </Card>
        <Card size="small" title="SharedTask（可选白板，不调度执行）">
          {sharedTasks.length ? sharedTasks.map(task => (
            <div key={task.id} className="mb-2 rounded border p-2 text-sm">
              <Space><Tag>{task.status}</Tag><b>{task.title}</b></Space>
              <div className="mt-1 text-muted-foreground">{task.description}</div>
            </div>
          )) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="模型尚未按需创建 SharedTask" />}
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card size="small" title="Working Plan">
          {plans.length ? plans.map(plan => <div key={plan.id} className="mb-2"><Tag>{plan.status}</Tag>{plan.summary}</div>)
            : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无计划" />}
        </Card>
        <Card size="small" title="Artifacts">
          {artifacts.length ? artifacts.map(artifact => (
            <Button key={artifact.id} type="link" block className="!text-left" onClick={() => onDownload(artifact)}>
              {artifact.name} · {artifact.size_bytes} B
            </Button>
          )) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无 Artifact" />}
        </Card>
        <Card size="small" title="Required Actions">
          {requiredActions.length ? requiredActions.map(action => (
            <div key={action.id} className="mb-2 rounded border p-2 text-sm"><Tag color="warning">{action.type}</Tag>{action.status}</div>
          )) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无需用户处理" />}
        </Card>
      </div>
    </div>
  )
}
