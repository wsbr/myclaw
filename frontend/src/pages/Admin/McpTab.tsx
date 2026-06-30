import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Table, Button, Form, Input, Select, Modal, Tag, message, Space } from 'antd'
import { PlusOutlined, CheckCircleOutlined } from '@ant-design/icons'
import { useState } from 'react'
import client from '../../api/client'

export default function McpTab() {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()

  const { data, isLoading } = useQuery({
    queryKey: ['mcp-servers'],
    queryFn: () => client.get('/mcp-servers').then(r => r.data),
  })

  const createMutation = useMutation({
    mutationFn: (v: any) => client.post('/mcp-servers', v).then(r => r.data),
    onSuccess: () => { message.success('MCP Server 已添加'); qc.invalidateQueries({ queryKey: ['mcp-servers'] }); setOpen(false); form.resetFields() },
  })

  const healthMutation = useMutation({
    mutationFn: (id: string) => client.post(`/mcp-servers/${id}/health`).then(r => r.data),
    onSuccess: (data) => message.info(`健康检查: ${data.status}`),
  })

  const columns = [
    { title: '名称', dataIndex: 'display_name', key: 'name' },
    { title: 'Endpoint', dataIndex: 'endpoint', key: 'endpoint', ellipsis: true },
    { title: '认证', dataIndex: 'auth_type', key: 'auth_type', render: (v: string) => <Tag>{v}</Tag> },
    { title: '能力', dataIndex: 'capability_summary', key: 'cap', ellipsis: true },
    { title: '状态', dataIndex: 'is_active', key: 'active', render: (v: boolean) => <Tag color={v ? 'success' : 'default'}>{v ? '启用' : '禁用'}</Tag> },
    {
      title: '操作', key: 'actions',
      render: (_: any, r: any) => (
        <Button size="small" icon={<CheckCircleOutlined />} onClick={() => healthMutation.mutate(r.id)}>健康检查</Button>
      ),
    },
  ]

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>注册 MCP Server</Button>
      </div>
      <Table dataSource={data || []} columns={columns} rowKey="id" loading={isLoading} />
      <Modal title="注册 MCP Server" open={open} onCancel={() => setOpen(false)} onOk={() => form.submit()}>
        <Form form={form} onFinish={createMutation.mutate} layout="vertical">
          <Form.Item name="name" label="唯一标识" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="display_name" label="显示名" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="endpoint" label="Endpoint URL" rules={[{ required: true }]}><Input placeholder="http://mcp.internal:8080" /></Form.Item>
          <Form.Item name="auth_type" label="认证方式" initialValue="none">
            <Select options={[{ value: 'none', label: '无认证' }, { value: 'bearer', label: 'Bearer Token' }, { value: 'api_key', label: 'API Key' }]} />
          </Form.Item>
          <Form.Item name="health_check_url" label="健康检查 URL"><Input /></Form.Item>
          <Form.Item name="capability_summary" label="能力描述"><Input.TextArea rows={2} /></Form.Item>
        </Form>
      </Modal>
    </>
  )
}
