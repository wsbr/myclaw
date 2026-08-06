import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Form, Input, Modal, Select, Space, Table, Tag, Typography, message } from 'antd'
import { ApiOutlined, CheckCircleOutlined, PlusOutlined } from '@ant-design/icons'
import { useState } from 'react'
import client from '../../api/client'

type LogicalModel = {
  id: string
  key: string
  name: string
  current_version_id: string | null
  status: string
  version: number
}

type ConformanceRun = {
  id: string
  suite_version: string
  status: 'passed' | 'failed' | 'running'
  result_summary: Record<string, unknown>
}

type ModelVersion = {
  id: string
  model_id: string
  version_no: number
  provider: string
  model_name: string
  endpoint?: string | null
  credential_ref?: string | null
  default_parameters: Record<string, unknown>
  capabilities: Record<string, unknown>
  latest_conformance: ConformanceRun | null
  content_hash: string
  published_at: string
}

const providers = ['openai', 'anthropic', 'openai_compatible', 'azure_openai', 'ollama'].map(value => ({ value, label: value }))
const commandHeaders = () => ({ 'Idempotency-Key': crypto.randomUUID() })

function parseObject(value?: string) {
  if (!value?.trim()) return {}
  return JSON.parse(value)
}

export default function ModelsTab() {
  const queryClient = useQueryClient()
  const [logicalForm] = Form.useForm()
  const [versionForm] = Form.useForm()
  const [selected, setSelected] = useState<LogicalModel | null>(null)
  const [logicalOpen, setLogicalOpen] = useState(false)
  const [versionOpen, setVersionOpen] = useState(false)

  const models = useQuery({
    queryKey: ['models-v2'],
    queryFn: () => client.get('/admin/models').then(response => response.data.items as LogicalModel[]),
  })
  const versions = useQuery({
    queryKey: ['model-versions-v2', selected?.id],
    queryFn: () => client.get(`/admin/models/${selected!.id}/versions`).then(response => response.data.items as ModelVersion[]),
    enabled: !!selected,
  })

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['models-v2'] })
    queryClient.invalidateQueries({ queryKey: ['model-versions-v2', selected?.id] })
  }

  const createLogical = useMutation({
    mutationFn: (values: { key: string; name: string }) =>
      client.post('/admin/models', values, { headers: commandHeaders() }).then(response => response.data as LogicalModel),
    onSuccess: model => {
      setLogicalOpen(false); logicalForm.resetFields(); setSelected(model); refresh()
      message.success('逻辑模型已创建；请追加不可变版本')
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || '逻辑模型创建失败'),
  })

  const createVersion = useMutation({
    mutationFn: (values: any) => client.post(`/admin/models/${selected!.id}/versions`, {
      ...values,
      endpoint: values.endpoint || null,
      credential_ref: values.credential_ref || null,
      default_parameters: parseObject(values.default_parameters),
      capabilities: parseObject(values.capabilities),
    }, { headers: { ...commandHeaders(), 'If-Match': `"${selected!.version}"` } }).then(response => response.data),
    onSuccess: () => {
      setVersionOpen(false); versionForm.resetFields(); refresh()
      message.success('不可变 ModelVersion 已创建')
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.message || error?.message || 'ModelVersion 创建失败'),
  })

  const runConformance = useMutation({
    mutationFn: (version: ModelVersion) => client.post(
      `/admin/models/${selected!.id}/versions/${version.id}/conformance`, {}, { headers: commandHeaders() },
    ).then(response => response.data as ConformanceRun),
    onSuccess: result => {
      refresh()
      message[result.status === 'passed' ? 'success' : 'error'](`Conformance ${result.status}`)
    },
    onError: () => message.error('Conformance 执行失败'),
  })

  const activate = useMutation({
    mutationFn: (version: ModelVersion) => client.post(
      `/admin/models/${selected!.id}/versions/${version.id}/activate`, {},
      { headers: { ...commandHeaders(), 'If-Match': `"${selected!.version}"` } },
    ).then(response => response.data as LogicalModel),
    onSuccess: model => {
      setSelected(model); refresh(); message.success('ModelVersion 已激活')
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.message || '只有最新 Conformance 通过的版本可激活'),
  })

  return <div className="space-y-4">
    <Alert showIcon type="info" message="Agent-native v2 模型注册表"
      description="逻辑模型可持续演进；运行与 Agent 发布只引用通过 Conformance 并激活的不可变 ModelVersion。" />
    <div className="flex justify-end">
      <Button type="primary" icon={<PlusOutlined />} onClick={() => setLogicalOpen(true)}>新建逻辑模型</Button>
    </div>
    <Table rowKey="id" loading={models.isLoading} dataSource={models.data ?? []} pagination={false}
      rowClassName={row => row.id === selected?.id ? 'bg-blue-50' : ''}
      onRow={row => ({ onClick: () => setSelected(row) })}
      columns={[
        { title: 'Key', dataIndex: 'key', render: (value: string) => <Typography.Text code>{value}</Typography.Text> },
        { title: '名称', dataIndex: 'name' },
        { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={value === 'active' ? 'success' : 'default'}>{value}</Tag> },
        { title: '当前版本', render: (_: unknown, row: LogicalModel) => row.current_version_id ? <Typography.Text code>{row.current_version_id.slice(0, 16)}</Typography.Text> : '未激活' },
        { title: '配置 revision', dataIndex: 'version' },
      ]} />

    {selected && <Card title={`${selected.name} · 不可变版本`}
      extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => {
        versionForm.setFieldsValue({
          default_parameters: JSON.stringify({ max_tokens: 2048 }, null, 2),
          capabilities: JSON.stringify({ streaming: true, native_tool_calling: true, tool_result_continuation: true, stop_reason: true }, null, 2),
        })
        setVersionOpen(true)
      }}>追加版本</Button>}>
      <Table rowKey="id" loading={versions.isLoading} dataSource={versions.data ?? []} pagination={false}
        expandable={{ expandedRowRender: version => <div className="space-y-2">
          <Typography.Paragraph copyable={{ text: version.content_hash }}><Typography.Text strong>内容哈希：</Typography.Text> {version.content_hash}</Typography.Paragraph>
          <Typography.Paragraph><Typography.Text strong>Endpoint：</Typography.Text> {version.endpoint || '官方默认'}</Typography.Paragraph>
          <Typography.Paragraph><Typography.Text strong>Credential：</Typography.Text> {version.credential_ref ? '已配置安全引用' : '未配置'}</Typography.Paragraph>
          <Typography.Paragraph><Typography.Text strong>默认参数：</Typography.Text> <Typography.Text code>{JSON.stringify(version.default_parameters)}</Typography.Text></Typography.Paragraph>
          <Typography.Paragraph><Typography.Text strong>能力：</Typography.Text> <Typography.Text code>{JSON.stringify(version.capabilities)}</Typography.Text></Typography.Paragraph>
          {version.latest_conformance && <Typography.Paragraph><Typography.Text strong>Conformance：</Typography.Text> {version.latest_conformance.suite_version} · {version.latest_conformance.status}</Typography.Paragraph>}
        </div> }}
        columns={[
          { title: '版本', dataIndex: 'version_no', render: (value: number) => `v${value}` },
          { title: 'Provider', dataIndex: 'provider', render: (value: string) => <Tag color="blue">{value}</Tag> },
          { title: '模型', dataIndex: 'model_name' },
          { title: 'Conformance', render: (_: unknown, version: ModelVersion) => version.latest_conformance
            ? <Tag color={version.latest_conformance.status === 'passed' ? 'success' : 'error'}>{version.latest_conformance.status}</Tag> : <Tag>未执行</Tag> },
          { title: '激活', render: (_: unknown, version: ModelVersion) => selected.current_version_id === version.id ? <Tag color="success">当前</Tag> : '—' },
          { title: '操作', render: (_: unknown, version: ModelVersion) => <Space>
            <Button size="small" icon={<ApiOutlined />} loading={runConformance.isPending} onClick={() => runConformance.mutate(version)}>运行 Conformance</Button>
            <Button size="small" type="primary" icon={<CheckCircleOutlined />}
              disabled={version.latest_conformance?.status !== 'passed'} loading={activate.isPending}
              onClick={() => activate.mutate(version)}>激活</Button>
          </Space> },
        ]} />
    </Card>}

    <Modal title="新建逻辑模型" open={logicalOpen} onCancel={() => setLogicalOpen(false)} onOk={() => logicalForm.submit()} confirmLoading={createLogical.isPending}>
      <Form form={logicalForm} layout="vertical" onFinish={values => createLogical.mutate(values)}>
        <Form.Item name="key" label="唯一 Key" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="name" label="显示名称" rules={[{ required: true }]}><Input /></Form.Item>
      </Form>
    </Modal>
    <Modal title={`追加 ${selected?.name ?? ''} 的不可变版本`} open={versionOpen} onCancel={() => setVersionOpen(false)} onOk={() => versionForm.submit()} confirmLoading={createVersion.isPending} width={720}>
      <Form form={versionForm} layout="vertical" onFinish={values => createVersion.mutate(values)}>
        <Space style={{ display: 'flex' }} align="start">
          <Form.Item name="provider" label="Provider" rules={[{ required: true }]}><Select options={providers} style={{ width: 190 }} /></Form.Item>
          <Form.Item name="model_name" label="Provider 模型名" rules={[{ required: true }]}><Input style={{ width: 260 }} /></Form.Item>
        </Space>
        <Form.Item name="endpoint" label="Endpoint"><Input placeholder="留空使用官方默认" /></Form.Item>
        <Form.Item name="credential_ref" label="凭证引用"><Input.Password placeholder="env://OPENAI_API_KEY" /></Form.Item>
        <Form.Item name="default_parameters" label="默认参数 JSON" rules={[{ validator: async (_, value) => { try { parseObject(value) } catch { throw new Error('请输入有效 JSON') } } }]}><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="capabilities" label="Provider 能力 JSON" rules={[{ required: true }, { validator: async (_, value) => { try { parseObject(value) } catch { throw new Error('请输入有效 JSON') } } }]}><Input.TextArea rows={6} /></Form.Item>
      </Form>
    </Modal>
  </div>
}
