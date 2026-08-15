import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Form, Input, Modal, Select, Space, Table, Tag, Typography, message } from 'antd'
import { ApiOutlined, CheckCircleOutlined, PlusOutlined } from '@ant-design/icons'
import { useEffect, useState } from 'react'
import client from '../../api/client'
import { displayLabel } from '@/lib/presentation'

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
  credential_configured: boolean
  credential_source?: 'encrypted' | 'deployment' | 'external' | 'legacy' | null
  default_parameters: Record<string, unknown>
  capabilities: Record<string, unknown>
  latest_conformance: ConformanceRun | null
  content_hash: string
  published_at: string
}

const providers = ['openai', 'anthropic', 'openai_compatible', 'azure_openai', 'ollama'].map(value => ({ value, label: value }))
const modelKinds = [
  { value: 'chat', label: '对话模型 / 智能体' },
  { value: 'embedding', label: '嵌入模型 / 知识库' },
]
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

  useEffect(() => {
    if (!models.data) return
    setSelected(current => {
      if (!current) return current
      return models.data.find(model => model.id === current.id) ?? current
    })
  }, [models.data])

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
    mutationFn: (values: any) => {
      const { model_kind: _modelKind, ...wireValues } = values
      return client.post(`/admin/models/${selected!.id}/versions`, {
      ...wireValues,
      endpoint: values.endpoint,
      api_key: values.api_key,
      default_parameters: parseObject(values.default_parameters),
      capabilities: parseObject(values.capabilities),
      }, { headers: { ...commandHeaders(), 'If-Match': `"${selected!.version}"` } }).then(response => response.data)
    },
    onSuccess: () => {
      setVersionOpen(false); versionForm.resetFields()
      setSelected(current => current ? { ...current, version: current.version + 1 } : current)
      refresh()
      message.success('不可变模型版本已创建')
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.message || error?.message || '模型版本创建失败'),
  })

  const runConformance = useMutation({
    mutationFn: (version: ModelVersion) => client.post(
      `/admin/models/${selected!.id}/versions/${version.id}/conformance`, {}, { headers: commandHeaders() },
    ).then(response => response.data as ConformanceRun),
    onSuccess: result => {
      refresh()
      message[result.status === 'passed' ? 'success' : 'error'](`兼容性验证：${displayLabel(result.status)}`)
    },
    onError: () => message.error('兼容性验证执行失败'),
  })

  const activate = useMutation({
    mutationFn: (version: ModelVersion) => client.post(
      `/admin/models/${selected!.id}/versions/${version.id}/activate`, {},
      { headers: { ...commandHeaders(), 'If-Match': `"${selected!.version}"` } },
    ).then(response => response.data as LogicalModel),
    onSuccess: model => {
      setSelected(model); refresh(); message.success('模型版本已激活')
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.message || '只有最新兼容性验证通过的版本可激活'),
  })

  return <div className="space-y-4">
    <Alert showIcon type="info" message="对话与嵌入模型使用统一注册表"
      description="逻辑模型可持续演进；智能体与知识库分别引用通过对应兼容性验证并激活的不可变模型版本。凭据可使用同一个供应商访问密钥。" />
    <div className="flex justify-end">
      <Button type="primary" icon={<PlusOutlined />} onClick={() => setLogicalOpen(true)}>新建逻辑模型</Button>
    </div>
    <Table rowKey="id" loading={models.isLoading} dataSource={models.data ?? []} pagination={false}
      rowClassName={row => row.id === selected?.id ? 'bg-blue-50' : ''}
      onRow={row => ({ onClick: () => setSelected(row) })}
      columns={[
        { title: '模型键', dataIndex: 'key', render: (value: string) => <Typography.Text code>{value}</Typography.Text> },
        { title: '名称', dataIndex: 'name' },
        { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={value === 'active' ? 'success' : 'default'}>{displayLabel(value)}</Tag> },
        { title: '当前版本', render: (_: unknown, row: LogicalModel) => row.current_version_id ? <Typography.Text code>{row.current_version_id.slice(0, 16)}</Typography.Text> : '未激活' },
        { title: '配置 revision', dataIndex: 'version' },
      ]} />

    {selected && <Card title={`${selected.name} · 不可变版本`}
      extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => {
        const current = versions.data?.find(version => version.id === selected.current_version_id)
        versionForm.setFieldsValue({
          provider: current?.provider,
          model_kind: current?.capabilities?.model_kind === 'embedding' ? 'embedding' : 'chat',
          model_name: current?.model_name,
          endpoint: current?.endpoint,
          api_key: '',
          default_parameters: JSON.stringify(current?.default_parameters ?? { max_tokens: 2048 }, null, 2),
          capabilities: JSON.stringify(current?.capabilities ?? { streaming: true, native_tool_calling: true, tool_result_continuation: true, stop_reason: true }, null, 2),
        })
        setVersionOpen(true)
      }}>追加版本</Button>}>
      <Table rowKey="id" loading={versions.isLoading} dataSource={versions.data ?? []} pagination={false}
        expandable={{ expandedRowRender: version => <div className="space-y-2">
          <Typography.Paragraph copyable={{ text: version.content_hash }}><Typography.Text strong>内容哈希：</Typography.Text> {version.content_hash}</Typography.Paragraph>
          <Typography.Paragraph><Typography.Text strong>接口地址：</Typography.Text> {version.endpoint || '未配置'}</Typography.Paragraph>
          <Typography.Paragraph><Typography.Text strong>访问密钥：</Typography.Text> {version.credential_configured ? `已加密保存（${version.credential_source ?? '平台存储'}）` : '未配置'}</Typography.Paragraph>
          <Typography.Paragraph><Typography.Text strong>默认参数：</Typography.Text> <Typography.Text code>{JSON.stringify(version.default_parameters)}</Typography.Text></Typography.Paragraph>
          <Typography.Paragraph><Typography.Text strong>能力：</Typography.Text> <Typography.Text code>{JSON.stringify(version.capabilities)}</Typography.Text></Typography.Paragraph>
          {version.latest_conformance && <Typography.Paragraph><Typography.Text strong>兼容性验证：</Typography.Text> {version.latest_conformance.suite_version} · {displayLabel(version.latest_conformance.status)}</Typography.Paragraph>}
        </div> }}
        columns={[
          { title: '版本', dataIndex: 'version_no', render: (value: number) => `v${value}` },
          { title: '服务商', dataIndex: 'provider', render: (value: string) => <Tag color="blue">{value}</Tag> },
          { title: '模型', dataIndex: 'model_name' },
          { title: '类型', render: (_: unknown, version: ModelVersion) => <Tag color={version.capabilities.model_kind === 'embedding' ? 'purple' : 'blue'}>
            {version.capabilities.model_kind === 'embedding' ? '嵌入模型' : '对话模型'}
          </Tag> },
          { title: '兼容性验证', render: (_: unknown, version: ModelVersion) => version.latest_conformance
            ? <Tag color={version.latest_conformance.status === 'passed' ? 'success' : 'error'}>{displayLabel(version.latest_conformance.status)}</Tag> : <Tag>未执行</Tag> },
          { title: '激活', render: (_: unknown, version: ModelVersion) => selected.current_version_id === version.id ? <Tag color="success">当前</Tag> : '—' },
          { title: '操作', render: (_: unknown, version: ModelVersion) => <Space>
            <Button size="small" icon={<ApiOutlined />} loading={runConformance.isPending} onClick={() => runConformance.mutate(version)}>运行兼容性验证</Button>
            <Button size="small" type="primary" icon={<CheckCircleOutlined />}
              disabled={version.latest_conformance?.status !== 'passed'} loading={activate.isPending}
              onClick={() => activate.mutate(version)}>激活</Button>
          </Space> },
        ]} />
    </Card>}

    <Modal title="新建逻辑模型" open={logicalOpen} onCancel={() => setLogicalOpen(false)} onOk={() => logicalForm.submit()} confirmLoading={createLogical.isPending}>
      <Form form={logicalForm} layout="vertical" onFinish={values => createLogical.mutate(values)}>
        <Form.Item name="key" label="唯一模型键" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="name" label="显示名称" rules={[{ required: true }]}><Input /></Form.Item>
      </Form>
    </Modal>
    <Modal title={`追加 ${selected?.name ?? ''} 的不可变版本`} open={versionOpen} onCancel={() => setVersionOpen(false)} onOk={() => versionForm.submit()} confirmLoading={createVersion.isPending} width={720}>
      <Form form={versionForm} layout="vertical" onFinish={values => createVersion.mutate(values)}>
        <Space style={{ display: 'flex' }} align="start">
          <Form.Item name="model_kind" label="模型类型" rules={[{ required: true }]}>
            <Select options={modelKinds} style={{ width: 190 }} onChange={kind => {
              if (kind === 'embedding') {
                versionForm.setFieldsValue({
                  provider: 'openai_compatible',
                  model_name: 'text-embedding-v4',
                  endpoint: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
                  default_parameters: JSON.stringify({ dimensions: 1024, encoding_format: 'float' }, null, 2),
                  capabilities: JSON.stringify({ model_kind: 'embedding', embedding_dimensions: 1024, embedding_max_batch_size: 10 }, null, 2),
                })
              } else {
                versionForm.setFieldsValue({
                  default_parameters: JSON.stringify({ max_tokens: 2048 }, null, 2),
                  capabilities: JSON.stringify({ model_kind: 'chat', streaming: true, native_tool_calling: true, tool_result_continuation: true, stop_reason: true }, null, 2),
                })
              }
            }} />
          </Form.Item>
          <Form.Item name="provider" label="服务商" rules={[{ required: true }]}><Select options={providers} style={{ width: 190 }} /></Form.Item>
          <Form.Item name="model_name" label="服务商模型名" rules={[{ required: true }]}><Input style={{ width: 220 }} /></Form.Item>
        </Space>
        <Form.Item name="endpoint" label="接口地址" rules={[{ required: true, message: '请输入模型 API 地址' }]}>
          <Input placeholder="https://api.example.com/v1" />
        </Form.Item>
        <Form.Item name="api_key" label="模型访问密钥" extra="完整密钥只提交一次；后端加密保存，页面与接口均不会回显。" rules={[{ required: true, message: '请输入完整模型访问密钥' }]}>
          <Input.Password autoComplete="new-password" placeholder="请输入完整模型访问密钥" />
        </Form.Item>
        <Form.Item name="default_parameters" label="默认参数 JSON" rules={[{ validator: async (_, value) => { try { parseObject(value) } catch { throw new Error('请输入有效 JSON') } } }]}><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="capabilities" label="服务商能力 JSON" rules={[{ required: true }, { validator: async (_, value) => { try { parseObject(value) } catch { throw new Error('请输入有效 JSON') } } }]}><Input.TextArea rows={6} /></Form.Item>
      </Form>
    </Modal>
  </div>
}
