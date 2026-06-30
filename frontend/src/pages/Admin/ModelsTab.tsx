import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Table, Button, Form, Input, Select, Modal, Tag, message, Popconfirm, Space, Alert, Divider } from 'antd'
import {
  PlusOutlined, DeleteOutlined, StopOutlined, CheckCircleOutlined, ApiOutlined,
} from '@ant-design/icons'
import { useState } from 'react'
import client from '../../api/client'

const PROVIDER_OPTIONS = [
  { value: 'anthropic',         label: 'Anthropic' },
  { value: 'openai',            label: 'OpenAI' },
  { value: 'openai_compatible', label: 'OpenAI Compatible（兼容协议）' },
  { value: 'azure_openai',      label: 'Azure OpenAI' },
  { value: 'ollama',            label: 'Ollama（本地）' },
]

// 常见 OpenAI-compatible 第三方服务预填
const COMPATIBLE_PRESETS: Record<string, { label: string; base_url: string }> = {
  deepseek:   { label: 'DeepSeek',    base_url: 'https://api.deepseek.com/v1' },
  moonshot:   { label: 'Moonshot',    base_url: 'https://api.moonshot.cn/v1' },
  minimax:    { label: 'MiniMax',     base_url: 'https://api.minimax.chat/v1' },
  groq:       { label: 'Groq',        base_url: 'https://api.groq.com/openai/v1' },
  together:   { label: 'Together AI', base_url: 'https://api.together.xyz/v1' },
  siliconflow:{ label: 'SiliconFlow', base_url: 'https://api.siliconflow.cn/v1' },
  zhipu:      { label: '智谱 GLM',    base_url: 'https://open.bigmodel.cn/api/paas/v4' },
  baichuan:   { label: '百川',        base_url: 'https://api.baichuan-ai.com/v1' },
}

function providerColor(v: string) {
  return v === 'anthropic' ? 'blue'
    : v === 'openai' ? 'green'
    : v === 'openai_compatible' ? 'cyan'
    : v === 'azure_openai' ? 'geekblue'
    : v === 'ollama' ? 'orange'
    : 'default'
}

export default function ModelsTab() {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const [selectedProvider, setSelectedProvider] = useState<string>('')
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; message: string }>>({})
  const [testingId, setTestingId] = useState<string | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['model-providers'],
    queryFn: () => client.get('/model-providers').then(r => r.data),
  })

  const createMutation = useMutation({
    mutationFn: (v: any) => client.post('/model-providers', v).then(r => r.data),
    onSuccess: () => {
      message.success('模型 Provider 已添加')
      qc.invalidateQueries({ queryKey: ['model-providers'] })
      setOpen(false)
      form.resetFields()
      setSelectedProvider('')
    },
    onError: (e: any) => {
      message.error(e?.response?.data?.detail || '添加失败')
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => client.delete(`/model-providers/${id}`),
    onSuccess: () => {
      message.success('已删除')
      qc.invalidateQueries({ queryKey: ['model-providers'] })
    },
    onError: () => message.error('删除失败'),
  })

  const activateMutation = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) =>
      client.patch(`/model-providers/${id}/${active ? 'activate' : 'deactivate'}`).then(r => r.data),
    onSuccess: (_data, { active }) => {
      message.success(active ? '已启用' : '已禁用')
      qc.invalidateQueries({ queryKey: ['model-providers'] })
    },
    onError: () => message.error('操作失败'),
  })

  const handleTest = async (id: string) => {
    setTestingId(id)
    try {
      const res = await client.post(`/model-providers/${id}/test`).then(r => r.data)
      setTestResults(prev => ({ ...prev, [id]: res }))
    } catch {
      setTestResults(prev => ({ ...prev, [id]: { ok: false, message: '请求失败' } }))
    } finally {
      setTestingId(null)
    }
  }

  const applyPreset = (key: string) => {
    const p = COMPATIBLE_PRESETS[key]
    if (!p) return
    form.setFieldsValue({ name: key, base_url: p.base_url })
  }

  const columns = [
    { title: '标识名', dataIndex: 'name', key: 'name' },
    {
      title: '提供商',
      dataIndex: 'provider',
      key: 'provider',
      render: (v: string) => <Tag color={providerColor(v)}>{v}</Tag>,
    },
    { title: 'Base URL', dataIndex: 'base_url', key: 'url', ellipsis: true },
    {
      title: '状态',
      dataIndex: 'is_active',
      key: 'active',
      render: (v: boolean) => <Tag color={v ? 'success' : 'default'}>{v ? '启用' : '禁用'}</Tag>,
    },
    {
      title: '操作',
      key: 'action',
      render: (_: any, record: any) => (
        <Space wrap>
          <Button
            size="small"
            icon={<ApiOutlined />}
            loading={testingId === record.id}
            onClick={() => handleTest(record.id)}
          >
            测试连接
          </Button>
          {record.is_active ? (
            <Button
              size="small"
              icon={<StopOutlined />}
              onClick={() => activateMutation.mutate({ id: record.id, active: false })}
            >
              禁用
            </Button>
          ) : (
            <Button
              size="small"
              type="primary"
              ghost
              icon={<CheckCircleOutlined />}
              onClick={() => activateMutation.mutate({ id: record.id, active: true })}
            >
              启用
            </Button>
          )}
          <Popconfirm
            title="确定删除此 Provider？"
            description="删除后数据不可恢复"
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => deleteMutation.mutate(record.id)}
          >
            <Button danger size="small" icon={<DeleteOutlined />}>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
          添加 Provider
        </Button>
      </div>

      {Object.entries(testResults).map(([id, result]) => {
        const provider = (data || []).find((p: any) => p.id === id)
        return (
          <Alert
            key={id}
            type={result.ok ? 'success' : 'error'}
            message={`${provider?.name ?? id}：${result.message}`}
            closable
            onClose={() => setTestResults(prev => { const n = { ...prev }; delete n[id]; return n })}
            style={{ marginBottom: 8 }}
          />
        )
      })}

      <Table dataSource={data || []} columns={columns} rowKey="id" loading={isLoading} />

      <Modal
        title="添加模型 Provider"
        open={open}
        onCancel={() => { setOpen(false); form.resetFields(); setSelectedProvider('') }}
        onOk={() => form.submit()}
        confirmLoading={createMutation.isPending}
      >
        <Form form={form} onFinish={createMutation.mutate} layout="vertical">
          <Form.Item name="provider" label="提供商类型" rules={[{ required: true, message: '请选择提供商' }]}>
            <Select
              options={PROVIDER_OPTIONS}
              onChange={v => { setSelectedProvider(v); form.setFieldValue('base_url', undefined) }}
            />
          </Form.Item>

          {/* 快捷预填：仅 openai_compatible 时显示 */}
          {selectedProvider === 'openai_compatible' && (
            <>
              <div style={{ marginBottom: 8 }}>
                <span style={{ fontSize: 12, color: '#888', marginRight: 8 }}>快速填入：</span>
                {Object.entries(COMPATIBLE_PRESETS).map(([key, p]) => (
                  <Tag
                    key={key}
                    color="cyan"
                    style={{ cursor: 'pointer', marginBottom: 4 }}
                    onClick={() => applyPreset(key)}
                  >
                    {p.label}
                  </Tag>
                ))}
              </div>
              <Divider style={{ margin: '8px 0' }} />
            </>
          )}

          <Form.Item name="name" label="唯一标识" rules={[{ required: true, message: '请填写唯一标识' }]}>
            <Input placeholder="e.g. my_deepseek" />
          </Form.Item>
          <Form.Item
            name="base_url"
            label="Base URL"
            rules={selectedProvider === 'openai_compatible' ? [{ required: true, message: 'OpenAI Compatible 必须填写 Base URL' }] : []}
          >
            <Input placeholder={
              selectedProvider === 'openai_compatible' ? 'https://api.deepseek.com/v1'
              : selectedProvider === 'ollama' ? 'http://localhost:11434'
              : selectedProvider === 'azure_openai' ? 'https://<resource>.openai.azure.com'
              : '留空使用官方默认'
            } />
          </Form.Item>
          <Form.Item name="api_key" label="API Key">
            <Input.Password placeholder="sk-..." />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
