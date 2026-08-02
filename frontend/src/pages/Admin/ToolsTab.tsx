import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Descriptions, Form, Input, Modal, Select, Space, Switch, Table, Tag, message } from 'antd'
import { ApiOutlined, EditOutlined, SyncOutlined } from '@ant-design/icons'
import { useState } from 'react'
import client from '../../api/client'

type PlatformTool = {
  id: string
  tool_key: string
  display_name: string
  description: string
  input_schema: Record<string, unknown>
  output_schema?: Record<string, unknown>
  implementation_ref: string
  implementation_version: string
  execution_mode: string
  enabled: boolean
  confirmation_policy: 'never' | 'always'
  platform_config: Record<string, unknown>
  config_version: number
}

const commandHeaders = (version?: number) => ({
  'Idempotency-Key': crypto.randomUUID(),
  ...(version === undefined ? {} : { 'If-Match': `"${version}"` }),
})

export default function ToolsTab() {
  const queryClient = useQueryClient()
  const [form] = Form.useForm()
  const [editing, setEditing] = useState<PlatformTool | null>(null)
  const [details, setDetails] = useState<PlatformTool | null>(null)

  const tools = useQuery({
    queryKey: ['platform-tools'],
    queryFn: () => client.get('/platform-tools').then(response => response.data.items as PlatformTool[]),
  })

  const update = useMutation({
    mutationFn: ({ tool, patch }: { tool: PlatformTool; patch: Record<string, unknown> }) =>
      client.patch(`/platform-tools/${tool.id}`, patch, { headers: commandHeaders(tool.config_version) }),
    onSuccess: () => {
      message.success('Tool 治理配置已更新')
      setEditing(null)
      queryClient.invalidateQueries({ queryKey: ['platform-tools'] })
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || '更新失败'),
  })

  const reconcile = useMutation({
    mutationFn: () => client.post('/platform-tools/reconcile', {}, { headers: commandHeaders() }).then(response => response.data),
    onSuccess: result => {
      message.success(`对账完成：新增 ${result.created}，更新 ${result.updated}`)
      queryClient.invalidateQueries({ queryKey: ['platform-tools'] })
    },
  })

  const openEditor = (tool: PlatformTool) => {
    setEditing(tool)
    form.setFieldsValue({ ...tool, platform_config: JSON.stringify(tool.platform_config, null, 2) })
  }

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button icon={<SyncOutlined />} loading={reconcile.isPending} onClick={() => reconcile.mutate()}>与代码注册表对账</Button>
      </div>
      <Table
        rowKey="id"
        loading={tools.isLoading}
        dataSource={tools.data || []}
        columns={[
          { title: 'Key', dataIndex: 'tool_key' },
          { title: '显示名', dataIndex: 'display_name' },
          { title: '执行模式', dataIndex: 'execution_mode', render: value => <Tag>{value}</Tag> },
          { title: '实现版本', dataIndex: 'implementation_version' },
          { title: '确认策略', dataIndex: 'confirmation_policy', render: (value, tool: PlatformTool) => <Select size="small" value={value} style={{ width: 100 }} options={[{ value: 'never' }, { value: 'always' }]} onChange={next => update.mutate({ tool, patch: { confirmation_policy: next } })} /> },
          { title: '启用', dataIndex: 'enabled', render: (value, tool: PlatformTool) => <Switch checked={value} onChange={next => update.mutate({ tool, patch: { enabled: next } })} /> },
          { title: '配置版本', dataIndex: 'config_version' },
          { title: '操作', render: (_, tool: PlatformTool) => <Space><Button size="small" icon={<EditOutlined />} onClick={() => openEditor(tool)}>编辑</Button><Button size="small" onClick={() => setDetails(tool)}>详情</Button><Button size="small" icon={<ApiOutlined />} onClick={async () => { const result = await client.post(`/platform-tools/${tool.id}/connection-test`).then(r => r.data); message[result.ok ? 'success' : 'warning'](result.ok ? 'Tool 可用' : result.provider_error_code) }}>测试</Button></Space> },
        ]}
      />
      <Modal title={`编辑 ${editing?.tool_key || ''}`} open={!!editing} onCancel={() => setEditing(null)} onOk={() => form.submit()} confirmLoading={update.isPending}>
        <Form form={form} layout="vertical" onFinish={values => {
          if (!editing) return
          try { update.mutate({ tool: editing, patch: { ...values, platform_config: JSON.parse(values.platform_config || '{}') } }) }
          catch { message.error('平台配置必须是有效 JSON') }
        }}>
          <Form.Item name="display_name" label="显示名" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="description" label="说明"><Input.TextArea rows={3} /></Form.Item>
          <Form.Item name="platform_config" label="平台配置 JSON"><Input.TextArea rows={6} /></Form.Item>
        </Form>
      </Modal>
      <Modal title={details?.tool_key} open={!!details} onCancel={() => setDetails(null)} footer={null} width={760}>
        {details && <Descriptions column={1} bordered size="small" items={[
          { key: 'impl', label: '实现', children: details.implementation_ref },
          { key: 'input', label: 'Input Schema', children: <pre style={{ whiteSpace: 'pre-wrap' }}>{JSON.stringify(details.input_schema, null, 2)}</pre> },
          { key: 'output', label: 'Output Schema', children: <pre style={{ whiteSpace: 'pre-wrap' }}>{JSON.stringify(details.output_schema || {}, null, 2)}</pre> },
        ]} />}
      </Modal>
    </>
  )
}
