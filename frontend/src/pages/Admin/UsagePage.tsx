import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'

import { adminApi, type UsageLedgerEntry } from '@/api/admin'
import { AdminSectionPage } from './AdminSectionPage'


const short = (value?: string | null) => value ? value.slice(0, 10) : '—'
const when = (value?: string | null) => value ? new Date(value).toLocaleString() : '—'

export default function UsagePage() {
  const queryClient = useQueryClient()
  const [groupBy, setGroupBy] = useState('run_id')
  const entries = useQuery({
    queryKey: ['usage-entries'],
    queryFn: () => adminApi.listUsageEntries({ limit: '100' }),
    refetchInterval: 5000,
  })
  const rollups = useQuery({
    queryKey: ['usage-rollups', groupBy],
    queryFn: () => adminApi.getUsageRollups({ group_by: groupBy }),
    refetchInterval: 5000,
  })
  const prices = useQuery({
    queryKey: ['usage-prices'],
    queryFn: () => adminApi.listUsagePrices(),
  })
  const createPrice = useMutation({
    mutationFn: adminApi.createUsagePrice,
    onSuccess: async () => {
      message.success('不可变价格快照已创建')
      await queryClient.invalidateQueries({ queryKey: ['usage-prices'] })
    },
    onError: () => message.error('价格窗口冲突或输入无效'),
  })
  const unknownEntries = entries.data?.items.filter(item => item.cost_status === 'unknown').length || 0

  return <AdminSectionPage
    title="Usage / 成本账本"
    description="按实际执行尝试追加用量，绑定生效时点价格；未知价格不会被静默当作零成本"
  >
    {unknownEntries > 0 && <Alert
      className="mb-4"
      type="warning"
      showIcon
      message={`${unknownEntries} 条当前记录缺少适用价格，成本保持 unknown`}
    />}

    <Card size="small" title="新增价格快照" className="mb-4">
      <Form
        layout="inline"
        onFinish={values => createPrice.mutate({
          ...values,
          subject_type: 'model',
          effective_from: values.effective_from.toISOString(),
          price_per_unit: String(values.price_per_unit),
        })}
      >
        <Form.Item name="provider" rules={[{ required: true }]}><Input placeholder="Provider" className="w-40" /></Form.Item>
        <Form.Item name="subject_key" rules={[{ required: true }]}><Input placeholder="Model name" className="w-44" /></Form.Item>
        <Form.Item name="metric" rules={[{ required: true }]}>
          <Select placeholder="Metric" className="w-44" options={['requests', 'input_tokens', 'cached_input_tokens', 'output_tokens', 'active_milliseconds'].map(value => ({ value }))} />
        </Form.Item>
        <Form.Item name="unit" initialValue="token" rules={[{ required: true }]}><Input placeholder="Unit" className="w-28" /></Form.Item>
        <Form.Item name="currency" initialValue="USD" rules={[{ required: true }]}><Input placeholder="USD" className="w-20" /></Form.Item>
        <Form.Item name="price_per_unit" rules={[{ required: true }]}><InputNumber stringMode min="0" step="0.000001" placeholder="单价" className="w-36" /></Form.Item>
        <Form.Item name="price_status" initialValue="contracted" rules={[{ required: true }]}>
          <Select className="w-32" options={['contracted', 'estimated'].map(value => ({ value }))} />
        </Form.Item>
        <Form.Item name="effective_from" rules={[{ required: true }]}><DatePicker showTime placeholder="生效时间" /></Form.Item>
        <Form.Item name="source_label" rules={[{ required: true }]}><Input placeholder="合同/估算来源" className="w-48" /></Form.Item>
        <Form.Item><Button type="primary" htmlType="submit" loading={createPrice.isPending}>创建快照</Button></Form.Item>
      </Form>
    </Card>

    <Card
      size="small"
      title="归集视图"
      className="mb-4"
      extra={<Select value={groupBy} onChange={setGroupBy} className="w-44" options={[
        ['run_id', '按 Run'], ['group_run_id', '按 GroupRun'],
        ['agent_version_id', '按 AgentVersion'], ['model_version_id', '按 ModelVersion'],
        ['account_id', '按 Account'],
      ].map(([value, label]) => ({ value, label }))} />}
    >
      <Table size="small" rowKey="key" loading={rollups.isLoading} pagination={false} dataSource={rollups.data?.items || []} columns={[
        { title: groupBy, dataIndex: 'key', render: short },
        { title: '条目', dataIndex: 'entry_count' },
        { title: '用量', dataIndex: 'quantities', render: value => <Typography.Text code>{JSON.stringify(value)}</Typography.Text> },
        { title: '已知成本', dataIndex: 'costs', render: value => <Typography.Text code>{JSON.stringify(value)}</Typography.Text> },
        { title: '未知成本条目', dataIndex: 'unknown_cost_entries', render: value => <Tag color={value ? 'warning' : 'success'}>{value}</Tag> },
      ]} />
    </Card>

    <div className="grid gap-4 xl:grid-cols-2">
      <Card size="small" title="最近账本条目">
        <Table<UsageLedgerEntry> size="small" rowKey="id" loading={entries.isLoading} pagination={false} scroll={{ x: 900 }} dataSource={entries.data?.items || []} columns={[
          { title: '时间', dataIndex: 'created_at', render: when },
          { title: 'Run', dataIndex: 'run_id', render: short },
          { title: '来源', dataIndex: 'source_type', render: value => <Tag>{value}</Tag> },
          { title: 'Metric', dataIndex: 'metric' },
          { title: 'Quantity', render: (_, row) => `${row.quantity} ${row.unit}` },
          { title: 'Cost', render: (_, row) => row.cost_status === 'unknown' ? <Tag color="warning">unknown</Tag> : `${row.currency} ${row.cost_amount} (${row.cost_status})` },
        ]} />
      </Card>
      <Card size="small" title="价格快照">
        <Table size="small" rowKey="id" loading={prices.isLoading} pagination={false} scroll={{ x: 700 }} dataSource={prices.data?.items || []} columns={[
          { title: 'Provider / Model', render: (_, row) => <Space direction="vertical" size={0}><span>{row.provider || '—'}</span><Typography.Text type="secondary">{row.subject_key}</Typography.Text></Space> },
          { title: 'Metric', render: (_, row) => `${row.metric} / ${row.unit}` },
          { title: '单价', render: (_, row) => `${row.currency} ${row.price_per_unit}` },
          { title: '口径', dataIndex: 'price_status', render: value => <Tag>{value}</Tag> },
          { title: '生效', dataIndex: 'effective_from', render: when },
          { title: '来源', dataIndex: 'source_label' },
        ]} />
      </Card>
    </div>
  </AdminSectionPage>
}
