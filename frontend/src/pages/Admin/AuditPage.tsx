import { useQuery } from '@tanstack/react-query'
import { Form, Input, Select, Space, Table, Tag } from 'antd'
import { useState } from 'react'
import client from '../../api/client'
import { AdminSectionPage } from './AdminSectionPage'

export default function AuditPage() {
  const [filters, setFilters] = useState<Record<string, string>>({})
  const events = useQuery({ queryKey: ['audit-events', filters], queryFn: () => client.get('/admin/audit-events', { params: filters }).then(r => r.data.items) })
  return <AdminSectionPage title="Platform Audit" description="查看无 Session 管理资源的不可变审计记录">
    <Form layout="inline" onValuesChange={(_, values) => setFilters(Object.fromEntries(Object.entries(values).filter(([, value]) => value)) as Record<string, string>)} style={{ marginBottom: 16 }}>
      <Form.Item name="resource_type"><Input placeholder="资源类型" allowClear /></Form.Item>
      <Form.Item name="actor_id"><Input placeholder="Actor ID" allowClear /></Form.Item>
      <Form.Item name="action"><Input placeholder="Action" allowClear /></Form.Item>
      <Form.Item name="outcome"><Select placeholder="结果" allowClear style={{ width: 120 }} options={[{ value: 'succeeded' }, { value: 'failed' }]} /></Form.Item>
    </Form>
    <Table rowKey="id" loading={events.isLoading} dataSource={events.data || []} columns={[
      { title: '时间', dataIndex: 'occurred_at', width: 180, render: value => new Date(value).toLocaleString() },
      { title: 'Action', dataIndex: 'action' },
      { title: 'Actor', render: (_, row: any) => <Space><Tag>{row.actor_type}</Tag>{row.actor_id || '—'}</Space> },
      { title: '资源', render: (_, row: any) => `${row.resource_type}:${row.resource_id}` },
      { title: '结果', dataIndex: 'outcome', render: value => <Tag color={value === 'succeeded' ? 'success' : 'error'}>{value}</Tag> },
      { title: '摘要', dataIndex: 'summary', ellipsis: true },
    ]} />
  </AdminSectionPage>
}
