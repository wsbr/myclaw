import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Descriptions, Form, Input, Modal, Select, Space, Switch, Table, Tabs, Tag, message } from 'antd'
import { ApiOutlined, EditOutlined, PlusOutlined, SyncOutlined } from '@ant-design/icons'
import { useState } from 'react'
import type { AxiosResponse } from 'axios'
import client from '../../api/client'
import { displayLabel, selectOptions } from '@/lib/presentation'

type ToolDefinition = {
  id: string; tool_key: string; namespace: string; origin: 'builtin' | 'mcp'
  display_name: string; display_name_zh: string; description: string
  tool_version_id: string; version_no: number; content_hash: string
  input_schema: Record<string, unknown>; output_schema: Record<string, unknown>
  examples: unknown[]; error_catalog: unknown[]; annotations: Record<string, unknown>
  implementation_ref: string; implementation_version: string
  execution_mode: string; side_effect: string; enabled: boolean
  confirmation_policy: 'never' | 'per_call' | 'session_grant'
  network_policy: Record<string, unknown>; workspace_policy: Record<string, unknown>
  risk_rules: unknown[]; config_version: number
}

type ToolConnection = {
  id: string; connection_key: string; display_name: string
  connection_type: 'bocha' | 'brave' | 'bailian' | 'mcp' | 'browser'
  auth_type: 'none' | 'bearer' | 'x_api_key'; status: string; enabled: boolean
  is_primary: boolean; endpoint: string; secret_configured: boolean
  secret_fingerprint: string | null; config: Record<string, unknown>
  revision_no: number; current_revision_id: string; version: number
  health: { status: string; checked_at: string | null; latency_ms: number | null; error_code: string | null }
}

type ToolPage = { items: ToolDefinition[]; next_cursor: string | null }

const commandHeaders = (version?: number) => ({
  'Idempotency-Key': crypto.randomUUID(),
  ...(version === undefined ? {} : { 'If-Match': `"${version}"` }),
})

export default function ToolsTab() {
  const queryClient = useQueryClient()
  const [toolForm] = Form.useForm()
  const [connectionForm] = Form.useForm()
  const [editing, setEditing] = useState<ToolDefinition | null>(null)
  const [details, setDetails] = useState<ToolDefinition | null>(null)
  const [connection, setConnection] = useState<ToolConnection | 'new' | null>(null)

  const tools = useQuery({
    queryKey: ['platform-tools'],
    queryFn: async () => {
      const items: ToolDefinition[] = []
      const seenCursors = new Set<string>()
      let cursor: string | null = null
      do {
        const response: AxiosResponse<ToolPage> = await client.get<ToolPage>('/platform-tools', {
          params: { limit: 100, ...(cursor ? { cursor } : {}) },
        })
        items.push(...response.data.items)
        cursor = response.data.next_cursor || null
        if (cursor && seenCursors.has(cursor)) throw new Error('platform_tools_cursor_cycle')
        if (cursor) seenCursors.add(cursor)
      } while (cursor)
      return items
    },
  })
  const connections = useQuery({
    queryKey: ['tool-connections'],
    queryFn: () => client.get('/tool-connections').then(response => response.data.items as ToolConnection[]),
  })

  const updateTool = useMutation({
    mutationFn: ({ tool, patch }: { tool: ToolDefinition; patch: Record<string, unknown> }) =>
      client.patch(`/platform-tools/${tool.id}`, patch, { headers: commandHeaders(tool.config_version) }),
    onSuccess: () => { message.success('工具治理已发布'); setEditing(null); queryClient.invalidateQueries({ queryKey: ['platform-tools'] }) },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || '更新失败'),
  })
  const saveConnection = useMutation({
    mutationFn: ({ current, values }: { current: ToolConnection | 'new'; values: any }) => {
      const payload = { ...values, config: JSON.parse(values.config || '{}') }
      return current === 'new'
        ? client.post('/tool-connections', payload, { headers: commandHeaders() })
        : client.patch(`/tool-connections/${current.id}`, payload, { headers: commandHeaders(current.version) })
    },
    onSuccess: () => { message.success('连接修订已保存，访问密钥不会回显'); setConnection(null); queryClient.invalidateQueries({ queryKey: ['tool-connections'] }) },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || '连接保存失败'),
  })
  const reconcile = useMutation({
    mutationFn: () => client.post('/platform-tools/reconcile', {}, { headers: commandHeaders() }).then(response => response.data),
    onSuccess: result => { message.success(`不可变目录已对账：新增 ${result.created}，新版本 ${result.updated}`); queryClient.invalidateQueries({ queryKey: ['platform-tools'] }) },
  })

  const openTool = (tool: ToolDefinition) => {
    setEditing(tool)
    toolForm.setFieldsValue({
      display_name: tool.display_name, display_name_zh: tool.display_name_zh,
      confirmation_policy: tool.confirmation_policy, enabled: tool.enabled,
      network_policy: JSON.stringify(tool.network_policy, null, 2),
      workspace_policy: JSON.stringify(tool.workspace_policy, null, 2),
      risk_rules: JSON.stringify(tool.risk_rules, null, 2),
    })
  }
  const openConnection = (current: ToolConnection | 'new') => {
    setConnection(current)
    connectionForm.resetFields()
    connectionForm.setFieldsValue(current === 'new' ? {
      connection_type: 'brave', auth_type: 'x_api_key', enabled: true, is_primary: false, config: '{}',
    } : { ...current, api_key: undefined, config: JSON.stringify(current.config, null, 2) })
  }

  const connectionTab = <>
    <Alert className="mb-3" type="info" showIcon message="平台统一凭证" description="访问密钥仅在保存时提交，数据库使用 AES-GCM 密文；接口、日志、执行轨迹和幂等快照均不会回显。连接测试会真实请求服务商。" />
    <div className="mb-3 flex justify-end"><Button icon={<PlusOutlined />} onClick={() => openConnection('new')}>新建连接</Button></div>
    <Table rowKey="id" loading={connections.isLoading} dataSource={connections.data || []} columns={[
      { title: '连接', render: (_, item: ToolConnection) => <><div>{item.display_name}</div><code className="text-xs">{item.connection_key}</code></> },
      { title: '类型', dataIndex: 'connection_type', render: value => <Tag>{value}</Tag> },
      { title: '修订', dataIndex: 'revision_no' },
      { title: '凭证', render: (_, item: ToolConnection) => item.secret_configured ? <Tag color="green">已加密 · {item.secret_fingerprint?.slice(0, 8)}</Tag> : <Tag>匿名</Tag> },
      { title: '主搜索', dataIndex: 'is_primary', render: value => value ? <Tag color="blue">主要</Tag> : null },
      { title: '健康', render: (_, item: ToolConnection) => <Tag color={item.health.status === 'healthy' ? 'green' : item.health.status === 'unhealthy' ? 'red' : 'default'}>{displayLabel(item.health.status)}{item.health.latency_ms != null ? ` · ${item.health.latency_ms} 毫秒` : ''}</Tag> },
      { title: '操作', render: (_, item: ToolConnection) => <Space>
        <Button size="small" icon={<EditOutlined />} onClick={() => openConnection(item)}>配置</Button>
        <Button size="small" icon={<ApiOutlined />} aria-label="真实测试" onClick={async () => {
          const result = await client.post(`/tool-connections/${item.id}/connection-test`).then(r => r.data)
          message[result.ok ? 'success' : 'error'](result.ok ? `真实连接成功 · ${result.latency_ms} 毫秒` : result.provider_error_code)
          queryClient.invalidateQueries({ queryKey: ['tool-connections'] })
        }}>真实测试</Button>
        {item.connection_type === 'mcp' && <Button size="small" icon={<SyncOutlined />} onClick={async () => {
          const result = await client.post(`/mcp-servers/${item.id}/sync`, {}, { headers: commandHeaders() }).then(r => r.data)
          message.success(`MCP 同步 ${result.tools.length} 个工具，默认待审核`)
          queryClient.invalidateQueries({ queryKey: ['platform-tools'] })
        }}>同步 MCP</Button>}
      </Space> },
    ]} />
  </>

  const toolTab = <>
    <div className="mb-3 flex justify-end"><Button icon={<SyncOutlined />} loading={reconcile.isPending} onClick={() => reconcile.mutate()}>发布代码工具版本</Button></div>
    <Table rowKey="id" loading={tools.isLoading} dataSource={tools.data || []} pagination={false} columns={[
      { title: '工具', render: (_, item: ToolDefinition) => <><div>{item.display_name_zh || item.display_name}</div><code className="text-xs">{item.tool_key}</code></> },
      { title: '来源', render: (_, item: ToolDefinition) => <Tag color={item.origin === 'mcp' ? 'purple' : 'blue'}>{item.namespace}</Tag> },
      { title: '版本', render: (_, item: ToolDefinition) => <span>v{item.version_no} <code>{item.content_hash?.slice(0, 8)}</code></span> },
      { title: '执行/风险', render: (_, item: ToolDefinition) => <Space direction="vertical" size={0}><Tag>{displayLabel(item.execution_mode)}</Tag><Tag>{displayLabel(item.side_effect)}</Tag></Space> },
      { title: '确认', dataIndex: 'confirmation_policy', render: value => <Tag color={value === 'per_call' ? 'orange' : value === 'session_grant' ? 'gold' : 'default'}>{displayLabel(value)}</Tag> },
      { title: '状态', dataIndex: 'enabled', render: value => <Tag color={value ? 'green' : 'default'}>{value ? '已启用' : '待审核/停用'}</Tag> },
      { title: '操作', render: (_, item: ToolDefinition) => <Space><Button size="small" onClick={() => openTool(item)}>治理</Button><Button size="small" aria-label={`查看 ${item.tool_key} 合同`} onClick={() => setDetails(item)}>合同</Button></Space> },
    ]} />
  </>

  return <>
    <Tabs items={[{ key: 'connections', label: '服务商 / MCP / 浏览器', children: connectionTab }, { key: 'catalog', label: '工具目录', children: toolTab }]} />
    <Modal title={`治理 ${editing?.tool_key || ''}`} open={!!editing} onCancel={() => setEditing(null)} onOk={() => toolForm.submit()} confirmLoading={updateTool.isPending}>
      <Form form={toolForm} layout="vertical" onFinish={values => {
        if (!editing) return
        try { updateTool.mutate({ tool: editing, patch: { ...values, network_policy: JSON.parse(values.network_policy), workspace_policy: JSON.parse(values.workspace_policy), risk_rules: JSON.parse(values.risk_rules) } }) }
        catch { message.error('治理策略必须是有效 JSON') }
      }}>
        <Form.Item name="display_name" label="英文显示名" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="display_name_zh" label="中文显示名（可选）">
          <Input placeholder="未配置时回退显示英文名称" />
        </Form.Item>
        <Form.Item name="confirmation_policy" label="默认确认策略"><Select options={selectOptions(['never', 'per_call', 'session_grant'])} /></Form.Item>
        <Form.Item name="enabled" label="管理员审核启用" valuePropName="checked"><Switch /></Form.Item>
        <Form.Item name="network_policy" label="网络策略"><Input.TextArea rows={3} /></Form.Item>
        <Form.Item name="workspace_policy" label="工作区策略"><Input.TextArea rows={3} /></Form.Item>
        <Form.Item name="risk_rules" label="动态风险规则"><Input.TextArea rows={3} /></Form.Item>
      </Form>
    </Modal>
    <Modal title={details?.tool_key} open={!!details} onCancel={() => setDetails(null)} footer={null} width={860}>
      {details && <Descriptions column={1} bordered size="small" items={[
        { key: 'identity', label: '不可变版本', children: `${details.tool_version_id} · ${details.content_hash}` },
        { key: 'impl', label: '实现', children: `${details.implementation_ref}@${details.implementation_version}` },
        { key: 'description', label: '模型说明', children: details.description },
        { key: 'input', label: '输入结构', children: <pre className="max-h-64 overflow-auto whitespace-pre-wrap">{JSON.stringify(details.input_schema, null, 2)}</pre> },
        { key: 'output', label: '输出结构', children: <pre className="max-h-64 overflow-auto whitespace-pre-wrap">{JSON.stringify(details.output_schema, null, 2)}</pre> },
        { key: 'errors', label: '错误目录', children: <pre className="whitespace-pre-wrap">{JSON.stringify(details.error_catalog, null, 2)}</pre> },
      ]} />}
    </Modal>
    <Modal title={connection === 'new' ? '新建工具连接' : `配置 ${(connection as ToolConnection | null)?.connection_key || ''}`} open={!!connection} onCancel={() => setConnection(null)} onOk={() => connectionForm.submit()} confirmLoading={saveConnection.isPending}>
      <Form form={connectionForm} layout="vertical" onValuesChange={changed => {
        if (connection === 'new' && changed.connection_type) {
          const type = changed.connection_type
          const searchProvider = ['bocha', 'brave', 'bailian'].includes(type)
          const providerDefaults: Record<string, { endpoint: string; config: string }> = {
            bocha: { endpoint: 'https://api.bochaai.com/v1/web-search', config: '{}' },
            brave: { endpoint: 'https://api.search.brave.com/res/v1/web/search', config: '{}' },
            bailian: {
              endpoint: 'https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation',
              config: JSON.stringify({ model: 'qwen-plus', search_strategy: 'turbo' }, null, 2),
            },
            mcp: { endpoint: '', config: '{}' },
          }
          connectionForm.setFieldsValue({
            auth_type: type === 'brave' ? 'x_api_key' : type === 'bocha' || type === 'bailian' ? 'bearer' : 'none',
            is_primary: searchProvider ? connectionForm.getFieldValue('is_primary') : false,
            ...(providerDefaults[type] || {}),
          })
        }
      }} onFinish={values => connection && saveConnection.mutate({ current: connection, values })}>
        {connection === 'new' && <Form.Item name="connection_key" label="稳定连接键" rules={[{ required: true }]}><Input placeholder="search.bailian.primary" /></Form.Item>}
        <Form.Item name="display_name" label="显示名" rules={[{ required: true }]}><Input /></Form.Item>
        {connection === 'new' && <Form.Item name="connection_type" label="类型"><Select options={['bocha', 'brave', 'bailian', 'mcp'].map(value => ({ value }))} /></Form.Item>}
        <Form.Item name="endpoint" label="HTTPS 接口地址" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="auth_type" label="认证"><Select options={['none', 'bearer', 'x_api_key'].map(value => ({ value }))} /></Form.Item>
        <Form.Item name="api_key" label="访问密钥（仅写入）" extra={(connection as ToolConnection)?.secret_configured ? '留空保留当前加密密钥；输入新值会创建新修订并轮换。' : undefined}><Input.Password autoComplete="new-password" /></Form.Item>
        <Form.Item name="config" label="类型化扩展配置 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Space><Form.Item name="enabled" valuePropName="checked"><Switch checkedChildren="启用" unCheckedChildren="停用" /></Form.Item><Form.Item name="is_primary" valuePropName="checked"><Switch checkedChildren="主搜索" unCheckedChildren="非主搜索" /></Form.Item></Space>
      </Form>
    </Modal>
  </>
}
