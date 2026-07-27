import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Table, Tag, Button, Space, Select, Badge, Tooltip, Card } from 'antd'
import { StopOutlined, ReloadOutlined } from '@ant-design/icons'
import { tasksApi } from '@/api/tasks'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import { useState } from 'react'

dayjs.extend(relativeTime)

const STATUS_COLORS: Record<string, string> = {
  pending: 'default', running: 'processing', waiting: 'warning',
  completed: 'success', failed: 'error', cancelled: 'default',
}

export default function TasksPage() {
  const queryClient = useQueryClient()
  const [statusFilter, setStatusFilter] = useState<string | undefined>()

  const { data: tasks, isLoading } = useQuery({
    queryKey: ['tasks', statusFilter],
    queryFn: () => tasksApi.list({ status: statusFilter }),
    refetchInterval: 5000,
  })

  const cancelMutation = useMutation({
    mutationFn: (id: string) => tasksApi.cancel(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tasks'] }),
  })

  const columns = [
    {
      title: '任务', dataIndex: 'title', key: 'title',
      render: (v: string, r: any) => (
        <div>
          <div className="font-medium">{v || r.id.slice(0, 12)}</div>
          {r.waiting_reason && (
            <div className="text-amber-600 text-xs">{r.waiting_reason}</div>
          )}
        </div>
      ),
    },
    {
      title: '状态', dataIndex: 'status', key: 'status',
      render: (v: string) => <Badge status={STATUS_COLORS[v] as any} text={<Tag color={STATUS_COLORS[v]}>{v}</Tag>} />,
    },
    { title: '渠道', dataIndex: 'output_channel', key: 'channel', render: (v: string) => <Tag>{v}</Tag> },
    {
      title: '触发', dataIndex: 'trigger_type', key: 'trigger',
      render: (v: string, r: any) => (
        <div>
          <Tag>{v}</Tag>
          {r.cron_expr && <div className="text-muted-foreground text-xs">{r.cron_expr}</div>}
        </div>
      ),
    },
    {
      title: '创建时间', dataIndex: 'created_at', key: 'created_at',
      render: (v: string) => <Tooltip title={v}>{dayjs(v).fromNow()}</Tooltip>,
    },
    {
      title: '完成时间', dataIndex: 'completed_at', key: 'completed_at',
      render: (v: string) => v ? <Tooltip title={v}>{dayjs(v).fromNow()}</Tooltip> : '-',
    },
    {
      title: '操作', key: 'actions',
      render: (_: any, r: any) => (
        <Space>
          {['pending', 'running', 'waiting'].includes(r.status) && (
            <Button size="small" danger icon={<StopOutlined />} onClick={() => cancelMutation.mutate(r.id)}>取消</Button>
          )}
        </Space>
      ),
    },
  ]

  return (
    <div className="mx-auto max-w-6xl">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight">Tasks</h2>
          <p className="text-muted-foreground mt-1 text-sm">异步任务看板，每 5 秒自动刷新</p>
        </div>
        <Space>
          <Select
            allowClear
            placeholder="筛选状态"
            style={{ width: 140 }}
            onChange={setStatusFilter}
            options={['pending','running','waiting','completed','failed','cancelled'].map(s => ({ label: s, value: s }))}
          />
          <Button icon={<ReloadOutlined />} onClick={() => queryClient.invalidateQueries({ queryKey: ['tasks'] })}>
            刷新
          </Button>
        </Space>
      </div>
      <Card className="rounded-xl shadow-sm" bordered={false}>
        <Table
          dataSource={tasks || []}
          columns={columns}
          rowKey="id"
          loading={isLoading}
          pagination={{ pageSize: 20 }}
        />
      </Card>
    </div>
  )
}
