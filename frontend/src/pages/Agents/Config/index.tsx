import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Tabs, Form, Input, Select, Button, Switch, Card, Space, Typography, message, Spin, Tag,
  InputNumber, Slider, Table, Popconfirm, Modal,
} from 'antd'
import {
  SaveOutlined, ArrowLeftOutlined, ThunderboltOutlined, PlusOutlined,
  CopyOutlined, DeleteOutlined,
} from '@ant-design/icons'
import { agentsApi } from '../../../api/agents'
import PromptEditor from '../../../components/PromptEditor'
import { useState } from 'react'
import client from '../../../api/client'

const MODES = ['chat', 'plan', 'group']
const CHANNELS = ['platform_ui', 'wechat', 'feishu', 'telegram', 'webhook', 'email']
const BUILTIN_TOOLS = [
  'create_task', 'create_scheduled_task', 'search_tasks', 'get_task_status',
  'send_to_channel', 'emit_event', 'find_agents', 'get_agent_schema', 'invoke_agent',
]

export default function AgentConfigPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [form] = Form.useForm()
  const isNew = !id || id === 'new'
  const [tokenModalOpen, setTokenModalOpen] = useState(false)
  const [newTokenName, setNewTokenName] = useState('')
  const [createdToken, setCreatedToken] = useState<string | null>(null)

  const { data: agent, isLoading } = useQuery({
    queryKey: ['agent', id],
    queryFn: () => agentsApi.get(id!),
    enabled: !isNew,
  })

  const { data: providers } = useQuery({
    queryKey: ['model-providers'],
    queryFn: () => client.get('/model-providers').then(r => r.data),
  })

  const { data: tools } = useQuery({
    queryKey: ['tools'],
    queryFn: () => client.get('/tools').then(r => r.data),
  })

  const { data: skills } = useQuery({
    queryKey: ['skills'],
    queryFn: () => client.get('/skills').then(r => r.data),
  })

  const { data: mcpServers } = useQuery({
    queryKey: ['mcp-servers'],
    queryFn: () => client.get('/mcp-servers').then(r => r.data),
  })

  const { data: apiTokens, refetch: refetchTokens } = useQuery({
    queryKey: ['agent-tokens', id],
    queryFn: () => agentsApi.listApiTokens(id!),
    enabled: !isNew,
  })

  const saveMutation = useMutation({
    mutationFn: (values: any) => isNew ? agentsApi.create(values) : agentsApi.update(id!, values),
    onSuccess: (saved) => {
      message.success('保存成功')
      queryClient.invalidateQueries({ queryKey: ['agents'] })
      queryClient.invalidateQueries({ queryKey: ['agent', id] })
      if (isNew) navigate(`/agents/${saved.id}/config`)
      else {
        const v = form.getFieldsValue()
        agentsApi.updateTools(id!, {
          tool_ids: v.tool_ids || [],
          builtin_tools: Object.fromEntries((v.builtin_tools || []).map((k: string) => [k, true])),
        })
        agentsApi.updateSkills(id!, { skill_ids: v.skill_ids || [] })
        agentsApi.updateMcpServers(id!, { mcp_server_ids: v.mcp_server_ids || [] })
      }
    },
    onError: () => message.error('保存失败'),
  })

  const activateMutation = useMutation({
    mutationFn: () => agentsApi.activate(id!),
    onSuccess: () => { message.success('已激活'); queryClient.invalidateQueries({ queryKey: ['agent', id] }) },
  })

  const duplicateMutation = useMutation({
    mutationFn: () => agentsApi.duplicate(id!),
    onSuccess: (copy: any) => { message.success('已复制'); navigate(`/agents/${copy.id}/config`) },
  })

  const createTokenMutation = useMutation({
    mutationFn: (name: string) => agentsApi.createApiToken(id!, name),
    onSuccess: (data: any) => { setCreatedToken(data.token); refetchTokens() },
  })

  const deleteTokenMutation = useMutation({
    mutationFn: (tokenId: string) => agentsApi.deleteApiToken(id!, tokenId),
    onSuccess: () => refetchTokens(),
  })

  if (!isNew && isLoading) return <Spin size="large" style={{ display: 'block', margin: '48px auto' }} />

  const mc = agent?.model_config_ || {}
  const cs = agent?.context_strategy || {}
  const mem = agent?.memory_config || {}

  const initialValues = agent ? {
    name: agent.name,
    description: agent.description,
    modes: agent.modes || ['chat'],
    supported_channels: agent.supported_channels || ['platform_ui'],
    identity: agent.system_prompt_blocks?.identity || '',
    instructions: agent.system_prompt_blocks?.instructions || '',
    context_block: agent.system_prompt_blocks?.context || '',
    output_format: agent.system_prompt_blocks?.output_format || '',
    provider_id: mc.provider_id,
    model_id: mc.model_id,
    temperature: mc.temperature ?? 0.7,
    max_tokens: mc.max_tokens ?? 8192,
    streaming: mc.streaming ?? true,
    extended_thinking: mc.extended_thinking ?? false,
    window_type: cs.window_type || 'sliding',
    max_ctx_tokens: cs.max_tokens || 128000,
    compression_threshold: cs.compression_threshold ?? 0.8,
    short_term: mem.short_term ?? true,
    long_term_enabled: mem.long_term?.enabled ?? false,
    memory_scope: mem.long_term?.scope || 'user',
    memory_ttl: mem.long_term?.ttl_days || 30,
    group_enabled: agent.group_config?.enabled || false,
    capability_manifest: JSON.stringify(agent.group_config?.capability_manifest || [], null, 2),
    event_manifest: JSON.stringify(agent.group_config?.event_manifest || [], null, 2),
    tool_ids: [], skill_ids: [], mcp_server_ids: [],
    builtin_tools: [],
    alert_error_rate: agent.alert_config?.alert_on_error_rate ?? 0.1,
    alert_latency: agent.alert_config?.alert_on_latency_p99 ?? 30,
    alert_cost: agent.alert_config?.alert_on_cost_daily ?? 100,
  } : {
    modes: ['chat'], supported_channels: ['platform_ui'],
    temperature: 0.7, max_tokens: 8192, streaming: true,
    window_type: 'sliding', max_ctx_tokens: 128000, compression_threshold: 0.8,
    short_term: true, long_term_enabled: false, memory_scope: 'user', memory_ttl: 30,
    group_enabled: false, capability_manifest: '[]', event_manifest: '[]',
    tool_ids: [], skill_ids: [], mcp_server_ids: [], builtin_tools: [],
    alert_error_rate: 0.1, alert_latency: 30, alert_cost: 100,
  }

  const onSave = () => {
    form.validateFields().then((v) => {
      saveMutation.mutate({
        name: v.name,
        description: v.description,
        modes: v.modes,
        supported_channels: v.supported_channels,
        system_prompt_blocks: {
          identity: v.identity,
          instructions: v.instructions,
          context: v.context_block,
          output_format: v.output_format,
        },
        model_config_: {
          provider_id: v.provider_id,
          model_id: v.model_id,
          temperature: v.temperature,
          max_tokens: v.max_tokens,
          streaming: v.streaming,
          extended_thinking: v.extended_thinking,
        },
        context_strategy: {
          window_type: v.window_type,
          max_tokens: v.max_ctx_tokens,
          compression_threshold: v.compression_threshold,
        },
        memory_config: {
          short_term: v.short_term,
          long_term: { enabled: v.long_term_enabled, scope: v.memory_scope, ttl_days: v.memory_ttl },
        },
        group_config: {
          enabled: v.group_enabled,
          capability_manifest: (() => { try { return JSON.parse(v.capability_manifest || '[]') } catch { return [] } })(),
          event_manifest: (() => { try { return JSON.parse(v.event_manifest || '[]') } catch { return [] } })(),
        },
        alert_config: {
          alert_on_error_rate: v.alert_error_rate,
          alert_on_latency_p99: v.alert_latency,
          alert_on_cost_daily: v.alert_cost,
        },
      })
    })
  }

  const tokenColumns = [
    { title: '名称', dataIndex: 'name', key: 'name' },
    { title: '最后使用', dataIndex: 'last_used_at', key: 'lu', render: (v: string) => v && v !== 'None' ? v.slice(0, 10) : '未使用' },
    { title: '过期', dataIndex: 'expires_at', key: 'exp', render: (v: string) => v && v !== 'None' ? v.slice(0, 10) : '永不' },
    {
      title: '', key: 'del',
      render: (_: any, r: any) => (
        <Popconfirm title="删除此 Token？" onConfirm={() => deleteTokenMutation.mutate(r.id)}>
          <Button size="small" danger icon={<DeleteOutlined />} />
        </Popconfirm>
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16, alignItems: 'center' }}>
        <Space>
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/agents')}>返回</Button>
          <Typography.Title level={4} style={{ margin: 0 }}>{isNew ? '新建 Agent' : `配置: ${agent?.name}`}</Typography.Title>
          {agent && <Tag color={agent.status === 'active' ? 'success' : 'default'}>{agent.status}</Tag>}
        </Space>
        <Space>
          {!isNew && <Button icon={<CopyOutlined />} onClick={() => duplicateMutation.mutate()}>复制</Button>}
          {agent?.status === 'draft' && <Button onClick={() => activateMutation.mutate()}>激活</Button>}
          {!isNew && (
            <Button icon={<ThunderboltOutlined />} onClick={() => navigate(`/agents/${id}/chat`)}>对话</Button>
          )}
          <Button type="primary" icon={<SaveOutlined />} onClick={onSave} loading={saveMutation.isPending}>保存</Button>
        </Space>
      </div>

      <Form form={form} initialValues={initialValues} layout="vertical">
        <Tabs defaultActiveKey="basic">

          <Tabs.TabPane tab="基础信息" key="basic">
            <Card>
              <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item>
              <Form.Item name="description" label="描述" rules={[{ required: true }]}>
                <Input.TextArea rows={3} />
              </Form.Item>
              <Form.Item name="modes" label="执行模式">
                <Select mode="multiple" options={MODES.map(m => ({ label: m, value: m }))} />
              </Form.Item>
              <Form.Item name="supported_channels" label="输出渠道">
                <Select mode="multiple" options={CHANNELS.map(c => ({ label: c, value: c }))} />
              </Form.Item>
            </Card>
          </Tabs.TabPane>

          <Tabs.TabPane tab="系统提示词" key="prompt">
            <Card>
              <Form.Item name="identity" label="Identity"><PromptEditor height={120} /></Form.Item>
              <Form.Item name="instructions" label="Instructions"><PromptEditor height={200} /></Form.Item>
              <Form.Item name="context_block" label="Context"><PromptEditor height={150} /></Form.Item>
              <Form.Item name="output_format" label="Output Format"><PromptEditor height={100} /></Form.Item>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                [skills] [tools] [group] 区块由平台自动注入，无需手动填写。
              </Typography.Text>
            </Card>
          </Tabs.TabPane>

          <Tabs.TabPane tab="模型配置" key="model">
            <Card>
              <Form.Item name="provider_id" label="模型 Provider" rules={[{ required: true }]}>
                <Select
                  placeholder="选择已配置的 Provider"
                  options={(providers || []).filter((p: any) => p.is_active).map((p: any) => ({ label: `${p.name} (${p.provider})`, value: p.id }))}
                />
              </Form.Item>
              <Form.Item name="model_id" label="模型 ID" rules={[{ required: true }]}>
                <Input placeholder="e.g. claude-sonnet-4-6, gpt-4o" />
              </Form.Item>
              <Form.Item name="temperature" label="Temperature">
                <Slider min={0} max={2} step={0.05} marks={{ 0: '0', 1: '1', 2: '2' }} />
              </Form.Item>
              <Form.Item name="max_tokens" label="Max Output Tokens">
                <InputNumber min={256} max={200000} style={{ width: '100%' }} />
              </Form.Item>
              <Space>
                <Form.Item name="streaming" label="流式输出" valuePropName="checked"><Switch /></Form.Item>
                <Form.Item name="extended_thinking" label="Extended Thinking（Anthropic）" valuePropName="checked"><Switch /></Form.Item>
              </Space>
            </Card>
          </Tabs.TabPane>

          <Tabs.TabPane tab="工具与能力" key="tools">
            <Card title="平台内置工具" style={{ marginBottom: 12 }}>
              <Form.Item name="builtin_tools">
                <Select mode="multiple" placeholder="按需开启" options={BUILTIN_TOOLS.map(k => ({ label: k, value: k }))} />
              </Form.Item>
            </Card>
            <Card title="自定义 Tool" style={{ marginBottom: 12 }}>
              <Form.Item name="tool_ids">
                <Select mode="multiple" placeholder="从 Tool Registry 选配"
                  options={(tools || []).map((t: any) => ({ label: t.display_name, value: t.id }))} />
              </Form.Item>
            </Card>
            <Card title="Skill（提示词片段）" style={{ marginBottom: 12 }}>
              <Form.Item name="skill_ids">
                <Select mode="multiple" placeholder="选配 Skill"
                  options={(skills || []).map((s: any) => ({ label: s.display_name, value: s.id }))} />
              </Form.Item>
            </Card>
            <Card title="MCP Server">
              <Form.Item name="mcp_server_ids">
                <Select mode="multiple" placeholder="选配外部 MCP Server"
                  options={(mcpServers || []).map((m: any) => ({ label: m.display_name, value: m.id }))} />
              </Form.Item>
            </Card>
          </Tabs.TabPane>

          <Tabs.TabPane tab="上下文策略" key="context">
            <Card>
              <Form.Item name="window_type" label="历史窗口策略">
                <Select options={[
                  { value: 'sliding', label: '滑动窗口（自动截断旧消息）' },
                  { value: 'summary', label: '摘要压缩（旧消息转摘要）' },
                  { value: 'full', label: '全量（不裁剪，适合短对话）' },
                ]} />
              </Form.Item>
              <Form.Item name="max_ctx_tokens" label="最大上下文 Token 数">
                <InputNumber min={4096} max={1000000} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="compression_threshold" label="触发压缩阈值（占满多少比例触发）">
                <Slider min={0.5} max={1} step={0.05} marks={{ 0.5: '50%', 0.8: '80%', 1: '100%' }} />
              </Form.Item>
            </Card>
          </Tabs.TabPane>

          <Tabs.TabPane tab="记忆策略" key="memory">
            <Card>
              <Form.Item name="short_term" label="短期记忆（Session 内，Redis）" valuePropName="checked">
                <Switch />
              </Form.Item>
              <Form.Item name="long_term_enabled" label="长期记忆（跨 Session，MySQL）" valuePropName="checked">
                <Switch />
              </Form.Item>
              <Form.Item name="memory_scope" label="记忆可见范围">
                <Select options={[
                  { value: 'user', label: 'User（用户级别）' },
                  { value: 'agent', label: 'Agent（Agent 级别）' },
                  { value: 'global', label: 'Global（全平台共享）' },
                ]} />
              </Form.Item>
              <Form.Item name="memory_ttl" label="有效期（天，0=永久）">
                <InputNumber min={0} max={365} style={{ width: '100%' }} />
              </Form.Item>
            </Card>
          </Tabs.TabPane>

          <Tabs.TabPane tab="Group 配置" key="group">
            <Card>
              <Form.Item name="group_enabled" label="加入 Group 工作网" valuePropName="checked"><Switch /></Form.Item>
              <Form.Item name="capability_manifest" label="能力清单（JSON 数组）">
                <PromptEditor height={200} language="json" />
              </Form.Item>
              <Form.Item name="event_manifest" label="事件清单（JSON 数组）">
                <PromptEditor height={150} language="json" />
              </Form.Item>
            </Card>
          </Tabs.TabPane>

          <Tabs.TabPane tab="API Token" key="tokens" disabled={isNew}>
            <Card
              title="外部调用令牌"
              extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => { setTokenModalOpen(true); setCreatedToken(null); setNewTokenName('') }}>新建</Button>}
            >
              <Table dataSource={apiTokens || []} columns={tokenColumns} rowKey="id" size="small" pagination={false} />
            </Card>
            <Modal
              title="新建 API Token"
              open={tokenModalOpen}
              onOk={() => !createdToken && createTokenMutation.mutate(newTokenName)}
              onCancel={() => setTokenModalOpen(false)}
              okText={createdToken ? '关闭' : '创建'}
            >
              {createdToken ? (
                <Space direction="vertical" style={{ width: '100%' }}>
                  <Typography.Text type="warning">Token 只显示一次，请立即复制保存：</Typography.Text>
                  <Input.TextArea value={createdToken} rows={3} readOnly style={{ fontFamily: 'monospace', fontSize: 12 }} />
                </Space>
              ) : (
                <Input value={newTokenName} onChange={e => setNewTokenName(e.target.value)} placeholder="Token 名称，如：生产环境" />
              )}
            </Modal>
          </Tabs.TabPane>

          <Tabs.TabPane tab="统计与告警" key="stats" disabled={isNew}>
            <Card title="告警阈值配置">
              <Form.Item name="alert_error_rate" label="错误率告警（超过此值告警，0~1）">
                <Slider min={0} max={1} step={0.01} marks={{ 0: '0%', 0.1: '10%', 0.5: '50%' }} />
              </Form.Item>
              <Form.Item name="alert_latency" label="P99 延迟告警（秒）">
                <InputNumber min={1} max={300} style={{ width: '100%' }} addonAfter="秒" />
              </Form.Item>
              <Form.Item name="alert_cost" label="日均费用告警">
                <InputNumber min={0} style={{ width: '100%' }} addonBefore="$" />
              </Form.Item>
            </Card>
          </Tabs.TabPane>

        </Tabs>
      </Form>
    </div>
  )
}
