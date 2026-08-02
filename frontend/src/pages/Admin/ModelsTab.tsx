import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Form, Input, Modal, Select, Space, Switch, Table, Tag, message } from 'antd'
import { ApiOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons'
import { useState } from 'react'
import client from '../../api/client'

type Model = {
  id: string
  key: string
  provider: string
  model_name: string
  endpoint?: string
  credential_configured: boolean
  default_parameters: Record<string, unknown>
  capabilities: Record<string, unknown>
  enabled: boolean
  version: number
}

const providers = ['openai', 'anthropic', 'openai_compatible'].map(value => ({ value, label: value }))
const commandHeaders = () => ({ 'Idempotency-Key': crypto.randomUUID() })

function parseObject(value?: string) {
  if (!value?.trim()) return {}
  return JSON.parse(value)
}

export default function ModelsTab() {
  const queryClient = useQueryClient()
  const [form] = Form.useForm()
  const [editing, setEditing] = useState<Model | null>(null)
  const [open, setOpen] = useState(false)
  const [testResult, setTestResult] = useState<{ id: string; ok: boolean; latency_ms: number; provider_error_code?: string }>()

  const models = useQuery({
    queryKey: ['models'],
    queryFn: () => client.get('/models').then(response => response.data.items as Model[]),
  })

  const save = useMutation({
    mutationFn: async (values: any) => {
      const payload = {
        ...values,
        default_parameters: parseObject(values.default_parameters),
        capabilities: parseObject(values.capabilities),
      }
      if (editing) {
        delete payload.key
        delete payload.provider
        delete payload.model_name
        return client.patch(`/models/${editing.id}`, payload, {
          headers: { ...commandHeaders(), 'If-Match': `"${editing.version}"` },
        }).then(response => response.data)
      }
      return client.post('/models', payload, { headers: commandHeaders() }).then(response => response.data)
    },
    onSuccess: () => {
      message.success(editing ? '模型配置已更新' : '模型已创建')
      setOpen(false)
      setEditing(null)
      form.resetFields()
      queryClient.invalidateQueries({ queryKey: ['models'] })
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || error?.message || '保存失败'),
  })

  const toggle = useMutation({
    mutationFn: ({ model, enabled }: { model: Model; enabled: boolean }) =>
      client.post(`/models/${model.id}/${enabled ? 'enable' : 'disable'}`, {}, { headers: commandHeaders() }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['models'] }),
    onError: () => message.error('状态更新失败'),
  })

  const testConnection = async (model: Model) => {
    const result = await client.post(`/models/${model.id}/connection-test`).then(response => response.data)
    setTestResult({ id: model.id, ...result })
  }

  const edit = (model: Model) => {
    setEditing(model)
    form.setFieldsValue({
      ...model,
      default_parameters: JSON.stringify(model.default_parameters, null, 2),
      capabilities: JSON.stringify(model.capabilities, null, 2),
    })
    setOpen(true)
  }

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => { setEditing(null); form.resetFields(); setOpen(true) }}>
          新建模型
        </Button>
      </div>
      {testResult && (
        <Alert
          closable
          style={{ marginBottom: 12 }}
          type={testResult.ok ? 'success' : 'error'}
          message={`${testResult.ok ? '连接可用' : '连接失败'} · ${testResult.latency_ms} ms${testResult.provider_error_code ? ` · ${testResult.provider_error_code}` : ''}`}
          onClose={() => setTestResult(undefined)}
        />
      )}
      <Table
        rowKey="id"
        loading={models.isLoading}
        dataSource={models.data || []}
        columns={[
          { title: 'Key', dataIndex: 'key' },
          { title: 'Provider', dataIndex: 'provider', render: value => <Tag color="blue">{value}</Tag> },
          { title: '模型', dataIndex: 'model_name' },
          { title: 'Endpoint', dataIndex: 'endpoint', ellipsis: true, render: value => value || '官方默认' },
          { title: '凭证', dataIndex: 'credential_configured', render: value => <Tag color={value ? 'success' : 'default'}>{value ? '已配置引用' : '未配置'}</Tag> },
          { title: '版本', dataIndex: 'version' },
          { title: '启用', dataIndex: 'enabled', render: (enabled, model: Model) => <Switch checked={enabled} onChange={value => toggle.mutate({ model, enabled: value })} /> },
          { title: '操作', render: (_, model: Model) => <Space><Button size="small" icon={<EditOutlined />} onClick={() => edit(model)}>编辑</Button><Button size="small" icon={<ApiOutlined />} onClick={() => testConnection(model)}>测试</Button></Space> },
        ]}
      />
      <Modal title={editing ? '编辑模型' : '新建模型'} open={open} onCancel={() => setOpen(false)} onOk={() => form.submit()} confirmLoading={save.isPending} width={680}>
        <Form form={form} layout="vertical" onFinish={save.mutate} initialValues={{ enabled: true }}>
          <Space style={{ display: 'flex' }} align="start">
            <Form.Item name="key" label="唯一 Key" rules={[{ required: true }]}><Input disabled={!!editing} /></Form.Item>
            <Form.Item name="provider" label="Provider" rules={[{ required: true }]}><Select disabled={!!editing} options={providers} style={{ width: 180 }} /></Form.Item>
            <Form.Item name="model_name" label="模型名称" rules={[{ required: true }]}><Input disabled={!!editing} /></Form.Item>
          </Space>
          <Form.Item name="endpoint" label="Endpoint"><Input placeholder="留空使用官方默认" /></Form.Item>
          <Form.Item name="credential_ref" label="凭证引用"><Input.Password placeholder="env://OPENAI_API_KEY（不保存明文凭证）" /></Form.Item>
          <Form.Item name="default_parameters" label="默认参数 JSON" rules={[{ validator: async (_, value) => { try { parseObject(value) } catch { throw new Error('请输入有效 JSON') } } }]}><Input.TextArea rows={4} /></Form.Item>
          <Form.Item name="capabilities" label="能力 JSON" rules={[{ validator: async (_, value) => { try { parseObject(value) } catch { throw new Error('请输入有效 JSON') } } }]}><Input.TextArea rows={4} /></Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
        </Form>
      </Modal>
    </>
  )
}
