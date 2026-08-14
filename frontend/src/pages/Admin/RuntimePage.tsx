import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Alert,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd'

import { adminApi, type RuntimeRunProjection } from '@/api/admin'
import { AdminSectionPage } from './AdminSectionPage'


const total = (values?: Record<string, number>) => Object.values(values || {}).reduce((sum, value) => sum + value, 0)
const short = (value?: string | null) => value ? value.slice(0, 10) : '—'
const when = (value?: unknown) => typeof value === 'string' && value ? new Date(value).toLocaleString() : '—'
const duration = (seconds?: number | null) => {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
}
const statusColor = (status?: string) => {
  if (status === 'failed' || status === 'dead' || status === 'outcome_unknown' || status === 'stale') return 'error'
  if (status === 'completed' || status === 'succeeded' || status === 'active') return 'success'
  if (status?.startsWith('waiting') || status === 'retry_wait' || status === 'blocked') return 'warning'
  if (status === 'running' || status === 'claimed') return 'processing'
  return 'default'
}

export default function RuntimePage() {
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const summary = useQuery({
    queryKey: ['runtime-summary'],
    queryFn: adminApi.getRuntimeSummary,
    refetchInterval: 5000,
  })
  const runs = useQuery({
    queryKey: ['runtime-runs', filters],
    queryFn: () => adminApi.listRuntimeRuns(filters),
    refetchInterval: 5000,
  })
  const detail = useQuery({
    queryKey: ['runtime-run-detail', selectedRunId],
    queryFn: () => adminApi.getRuntimeRun(selectedRunId!),
    enabled: !!selectedRunId,
    refetchInterval: query => query.state.data && ['completed', 'cancelled', 'failed'].includes(query.state.data.status) ? false : 3000,
  })

  const data = summary.data
  const activeRuns = Object.entries(data?.runs || {}).filter(([status]) => !['completed', 'cancelled', 'failed'].includes(status)).reduce((sum, [, count]) => sum + count, 0)
  const queuedCommands = (data?.outbox || []).filter(item => item.status === 'pending' || item.status === 'retry_wait').reduce((sum, item) => sum + item.count, 0)
  const unknownOutcomes = (data?.outcome_unknown.provider_attempts || 0) + (data?.outcome_unknown.tool_attempts || 0)

  const runColumns = [
    {
      title: 'Run', dataIndex: 'id',
      render: (value: string, row: RuntimeRunProjection) => <Space direction="vertical" size={0}>
        <Typography.Text code>{short(value)}</Typography.Text>
        <Typography.Text type="secondary" className="text-xs">{short(row.session_id)} · {short(row.account_id)}</Typography.Text>
      </Space>,
    },
    { title: '模式', dataIndex: 'mode', render: (value: string) => <Tag>{value}</Tag> },
    {
      title: '状态',
      render: (_: unknown, row: RuntimeRunProjection) => <Space direction="vertical" size={0}>
        <Tag color={statusColor(row.status)}>{row.status}</Tag>
        <Typography.Text type="secondary" className="text-xs">{row.wait_reason || row.terminal_reason || '—'}</Typography.Text>
      </Space>,
    },
    { title: '年龄', dataIndex: 'age_seconds', render: duration },
    {
      title: '执行事实',
      render: (_: unknown, row: RuntimeRunProjection) => `W ${total(row.counts.work_items)} · R ${total(row.counts.runners)} · T ${total(row.counts.tool_invocations)} · A ${total(row.counts.agent_invocations)}`,
    },
    {
      title: '下一动作',
      render: (_: unknown, row: RuntimeRunProjection) => row.next_action
        ? <Space direction="vertical" size={0}>
          <Typography.Text>{row.next_action.actor_type}</Typography.Text>
          <Typography.Text type="secondary" className="text-xs">{short(row.next_action.owner_agent_id)} · {row.next_action.wait_reason || row.next_action.status}</Typography.Text>
        </Space> : '—',
    },
    {
      title: '预算预警',
      render: (_: unknown, row: RuntimeRunProjection) => row.budget.warning_dimensions.length
        ? row.budget.warning_dimensions.map(item => <Tag key={item} color="warning">{item}</Tag>) : <Tag color="success">正常</Tag>,
    },
    { title: '诊断', render: (_: unknown, row: RuntimeRunProjection) => <Tag color={row.counts.diagnostics ? 'orange' : 'default'}>{row.counts.diagnostics}</Tag> },
    { title: '更新时间', dataIndex: 'updated_at', render: when },
  ]

  const runtimeDetail = detail.data
  const recordTable = (items: Array<Record<string, unknown>>, columns: Array<Record<string, unknown>>, empty: string) => items.length
    ? <Table size="small" pagination={false} rowKey="id" dataSource={items} columns={columns as never} scroll={{ x: true }} />
    : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={empty} />

  return <AdminSectionPage
    title="Runtime Console"
    description="只读查看权威 Run、Runner、调用、队列、租约与诊断事实；不直接修改数据库状态"
  >
    {unknownOutcomes > 0 && <Alert className="mb-4" type="error" showIcon message={`${unknownOutcomes} 个执行结果处于 outcome_unknown，需要人工核对外部副作用`} />}

    <Row gutter={[16, 16]} className="mb-4">
      <Col xs={12} xl={6}><Card><Statistic title="非终态 Runs" value={activeRuns} /></Card></Col>
      <Col xs={12} xl={6}><Card><Statistic title="Queued Runners" value={data?.runners.queued || 0}
        suffix={data?.queue_age.oldest_queued_runner_seconds == null ? undefined : `最老 ${duration(data.queue_age.oldest_queued_runner_seconds)}`} /></Card></Col>
      <Col xs={12} xl={6}><Card><Statistic title="待处理 Outbox" value={queuedCommands} /></Card></Col>
      <Col xs={12} xl={6}><Card><Statistic title="Group 诊断" value={data?.diagnostic_count || 0} /></Card></Col>
    </Row>

    <Card size="small" title="Runs" className="mb-4" extra={data?.generated_at ? `投影于 ${when(data.generated_at)}` : undefined}>
      <Form
        layout="inline"
        className="mb-4"
        onValuesChange={(_, values) => setFilters(Object.fromEntries(Object.entries(values).filter(([, value]) => value)) as Record<string, string>)}
      >
        <Form.Item name="status"><Select allowClear placeholder="状态" className="w-44" options={['active', 'waiting_user', 'waiting_approval', 'waiting_tool', 'waiting_agent', 'completed', 'cancelled', 'failed'].map(value => ({ value }))} /></Form.Item>
        <Form.Item name="mode"><Select allowClear placeholder="模式" className="w-32" options={['chat', 'plan', 'group'].map(value => ({ value }))} /></Form.Item>
        <Form.Item name="account_id"><Input allowClear placeholder="Account ID" className="w-64" /></Form.Item>
      </Form>
      <Table
        size="small"
        rowKey="id"
        loading={runs.isLoading}
        dataSource={runs.data?.items || []}
        columns={runColumns}
        pagination={false}
        scroll={{ x: 1100 }}
        onRow={row => ({ onClick: () => setSelectedRunId(row.id), className: 'cursor-pointer' })}
      />
    </Card>

    <div className="grid gap-4 xl:grid-cols-2">
      <Card size="small" title="Outbox 队列健康">
        <Table size="small" pagination={false} rowKey={row => `${row.topic}:${row.status}`} dataSource={data?.outbox || []} columns={[
          { title: 'Topic', dataIndex: 'topic' },
          { title: '状态', dataIndex: 'status', render: value => <Tag color={statusColor(value)}>{value}</Tag> },
          { title: '数量', dataIndex: 'count' },
          { title: '最老年龄', dataIndex: 'oldest_age_seconds', render: duration },
        ]} />
      </Card>
      <Card size="small" title="Worker 进程与槽位健康">
        <Table size="small" pagination={false} rowKey={row => `${row.worker_kind}:${row.worker_id}`} dataSource={data?.observed_workers || []} columns={[
          { title: '类型', dataIndex: 'worker_kind', render: value => <Tag>{value}</Tag> },
          { title: 'Worker', dataIndex: 'worker_id', ellipsis: true },
          { title: '槽位', render: (_, row) => row.capacity == null ? `${row.observed_active_slots}/?` : `${row.active_slots}/${row.capacity}` },
          { title: '健康', dataIndex: 'health', render: value => <Tag color={statusColor(value)}>{value}</Tag> },
          { title: '陈旧租约', dataIndex: 'stale_leases' },
          { title: '心跳', dataIndex: 'last_heartbeat_at', render: when },
        ]} />
      </Card>
    </div>

    <Drawer
      title={`Run 诊断 · ${short(selectedRunId)}`}
      width="86vw"
      open={!!selectedRunId}
      onClose={() => setSelectedRunId(null)}
      loading={detail.isLoading}
      destroyOnClose
    >
      {runtimeDetail && <Tabs items={[
        {
          key: 'overview', label: '概览 / 预算', children: <Space direction="vertical" className="w-full" size="large">
            <Descriptions bordered size="small" column={3}>
              <Descriptions.Item label="状态"><Tag color={statusColor(runtimeDetail.status)}>{runtimeDetail.status}</Tag></Descriptions.Item>
              <Descriptions.Item label="模式">{runtimeDetail.mode}</Descriptions.Item>
              <Descriptions.Item label="Account">{runtimeDetail.account_id || '—'}</Descriptions.Item>
              <Descriptions.Item label="等待原因">{runtimeDetail.wait_reason || '—'}</Descriptions.Item>
              <Descriptions.Item label="终态原因">{runtimeDetail.terminal_reason || '—'}</Descriptions.Item>
              <Descriptions.Item label="Deadline">{when(runtimeDetail.deadline_at)}</Descriptions.Item>
              <Descriptions.Item label="下一动作" span={3}>{runtimeDetail.next_action ? `${runtimeDetail.next_action.actor_type} · Agent ${short(runtimeDetail.next_action.owner_agent_id)} · ${runtimeDetail.next_action.wait_reason || runtimeDetail.next_action.status}` : '—'}</Descriptions.Item>
              <Descriptions.Item label="最近进度" span={3}>{runtimeDetail.last_progress ? `${runtimeDetail.last_progress.reason_code} · 连续无进展 ${runtimeDetail.last_progress.consecutive_no_progress}` : '—'}</Descriptions.Item>
            </Descriptions>
            <Table size="small" pagination={false} rowKey="dimension" dataSource={Object.entries(runtimeDetail.budget.limits).map(([key, limit]) => ({
              dimension: key.replace(/^max_/, ''), limit, used: runtimeDetail.budget.usage[key.replace(/^max_/, '')],
            }))} columns={[
              { title: '维度', dataIndex: 'dimension' },
              { title: '已用', dataIndex: 'used' },
              { title: '上限', dataIndex: 'limit' },
              { title: '利用率', render: (_, row) => row.used === undefined ? '—' : `${Math.round(row.used / row.limit * 100)}%` },
            ]} />
          </Space>,
        },
        {
          key: 'work', label: `Work / Runner (${runtimeDetail.work_items.length}/${runtimeDetail.runners.length})`, children: <Space direction="vertical" className="w-full" size="large">
            {recordTable(runtimeDetail.work_items, [
              { title: 'WorkItem', dataIndex: 'id', render: short }, { title: 'Agent', dataIndex: 'owner_agent_id', render: short }, { title: 'Purpose', dataIndex: 'purpose' }, { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> }, { title: '等待/错误', render: (_: unknown, row: Record<string, unknown>) => row.wait_reason || row.error_code || '—' }, { title: 'Epoch', dataIndex: 'execution_epoch' },
            ], '暂无 WorkItem')}
            {recordTable(runtimeDetail.runners, [
              { title: 'Runner', dataIndex: 'id', render: short }, { title: 'WorkItem', dataIndex: 'work_item_id', render: short }, { title: 'Agent', dataIndex: 'agent_id', render: short }, { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> }, { title: 'Epoch', dataIndex: 'execution_epoch' }, { title: 'Lease owner', dataIndex: 'lease_owner' }, { title: '心跳', dataIndex: 'heartbeat_at', render: when }, { title: '错误', render: (_: unknown, row: Record<string, unknown>) => row.failure_code || row.error_code || '—' },
            ], '暂无 Runner')}
          </Space>,
        },
        {
          key: 'calls', label: 'Provider / Tool / Agent', children: <Space direction="vertical" className="w-full" size="large">
            {recordTable(runtimeDetail.provider_attempts, [
              { title: 'ProviderAttempt', dataIndex: 'id', render: short }, { title: 'Provider', dataIndex: 'provider' }, { title: 'Attempt', dataIndex: 'attempt_no' }, { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> }, { title: 'Usage', dataIndex: 'usage', render: (value: unknown) => <Typography.Text code>{JSON.stringify(value)}</Typography.Text> }, { title: '错误', dataIndex: 'error_code' },
            ], '暂无 ProviderAttempt')}
            {recordTable(runtimeDetail.tool_invocations, [
              { title: 'ToolInvocation', dataIndex: 'id', render: short }, { title: 'Tool', dataIndex: 'tool_key' }, { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> }, { title: 'Attempts', dataIndex: 'attempt_count' }, { title: '错误', dataIndex: 'error_code' },
            ], '暂无 ToolInvocation')}
            {recordTable(runtimeDetail.agent_invocations, [
              { title: 'AgentInvocation', dataIndex: 'id', render: short }, { title: '路径', render: (_: unknown, row: Record<string, unknown>) => `${short(row.caller_agent_id as string)} → ${short(row.target_agent_id as string)}` }, { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> }, { title: 'Route', dataIndex: 'return_route' }, { title: '错误', dataIndex: 'error_code' },
            ], '暂无 AgentInvocation')}
          </Space>,
        },
        {
          key: 'timeline', label: `因果时间线 (${runtimeDetail.timeline.length})`, children: recordTable(runtimeDetail.timeline, [
            { title: '#', dataIndex: 'sequence_no' }, { title: '时间', dataIndex: 'occurred_at', render: when }, { title: 'Event', dataIndex: 'event_type' }, { title: 'Actor', render: (_: unknown, row: Record<string, unknown>) => `${row.actor_type}:${row.actor_id || '—'}` }, { title: '实体', render: (_: unknown, row: Record<string, unknown>) => `${row.entity_type}:${short(row.entity_id as string)}` }, { title: '摘要', dataIndex: 'summary' },
          ], '暂无事件'),
        },
        {
          key: 'evidence', label: `消息 / Turn / Checkpoint / Group 边 (${runtimeDetail.messages.length}/${runtimeDetail.agent_turns.length}/${runtimeDetail.checkpoints.length}/${runtimeDetail.group_edges.length})`, children: <Space direction="vertical" className="w-full" size="large">
            {recordTable(runtimeDetail.messages, [
              { title: '#', dataIndex: 'sequence_no' }, { title: '消息', dataIndex: 'id', render: short }, { title: '发送方', render: (_: unknown, row: Record<string, unknown>) => `${row.sender_type}:${short(row.sender_agent_id as string)}` }, { title: '接收 Agent', dataIndex: 'recipient_agent_id', render: short }, { title: '可见性', dataIndex: 'visibility' }, { title: 'Role', dataIndex: 'role' }, { title: '时间', dataIndex: 'created_at', render: when },
            ], '暂无已提交消息元数据')}
            {recordTable(runtimeDetail.agent_turns, [
              { title: 'Turn', dataIndex: 'id', render: short }, { title: 'Runner', dataIndex: 'runner_id', render: short }, { title: '#', dataIndex: 'turn_no' }, { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> }, { title: 'Stop', dataIndex: 'stop_reason' }, { title: 'Usage', dataIndex: 'usage', render: (value: unknown) => <Typography.Text code>{JSON.stringify(value)}</Typography.Text> },
            ], '暂无 AgentTurn')}
            {recordTable(runtimeDetail.checkpoints, [
              { title: 'Checkpoint', dataIndex: 'id', render: short }, { title: 'Runner', dataIndex: 'runner_id', render: short }, { title: '#', dataIndex: 'sequence_no' }, { title: '类型', dataIndex: 'checkpoint_type' }, { title: '可恢复', dataIndex: 'safe_to_resume', render: (value: boolean) => <Tag color={value ? 'success' : 'default'}>{String(value)}</Tag> }, { title: 'Hash', dataIndex: 'content_hash', render: short },
            ], '暂无 Checkpoint')}
            {recordTable(runtimeDetail.group_edges, [
              { title: '类型', dataIndex: 'cause_type' }, { title: '路径', render: (_: unknown, row: Record<string, unknown>) => `${short(row.source_agent_id as string)} → ${short(row.target_agent_id as string)}` }, { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> }, { title: 'Cause', dataIndex: 'cause_id', render: short },
            ], '暂无 Group 交互边')}
          </Space>,
        },
        {
          key: 'diagnostics', label: `诊断 / Outbox (${runtimeDetail.diagnostics.length}/${runtimeDetail.outbox.length})`, children: <Space direction="vertical" className="w-full" size="large">
            {recordTable(runtimeDetail.diagnostics, [
              { title: '时间', dataIndex: 'created_at', render: when }, { title: 'Code', dataIndex: 'diagnostic_code' }, { title: '类别', dataIndex: 'category' }, { title: '处置', dataIndex: 'disposition', render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> }, { title: '摘要', dataIndex: 'summary' },
            ], '暂无 Runtime 诊断')}
            {recordTable(runtimeDetail.outbox, [
              { title: '时间', dataIndex: 'created_at', render: when }, { title: 'Topic', dataIndex: 'topic' }, { title: 'Action', dataIndex: 'action' }, { title: '聚合', render: (_: unknown, row: Record<string, unknown>) => `${row.aggregate_type}:${short(row.aggregate_id as string)}` }, { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag> }, { title: 'Attempts', dataIndex: 'attempt_count' },
            ], '暂无关联 Outbox 命令')}
          </Space>,
        },
      ]} />}
    </Drawer>
  </AdminSectionPage>
}
