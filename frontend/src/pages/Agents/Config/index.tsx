import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Form, Input, Modal, Radio, Select, Space, Spin, Switch, Table, Tabs, Tag, Typography, message } from 'antd'
import { ArrowLeftOutlined, CloudUploadOutlined, ExperimentOutlined, KeyOutlined, PauseCircleOutlined, PlayCircleOutlined, SaveOutlined } from '@ant-design/icons'
import { agentsApi, AgentApiKey, AgentDraft, AgentOption, AgentPublishedVersion } from '@/api/agents'
import PromptEditor from '@/components/PromptEditor'

const policyTypes = ['context', 'history', 'memory_write', 'memory_recall'] as const

export default function AgentConfigPage() {
  const { id } = useParams<{ id: string }>()
  const isNew = !id || id === 'new'
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [form] = Form.useForm()
  const [keyForm] = Form.useForm()
  const [testOpen, setTestOpen] = useState(false)
  const [testContent, setTestContent] = useState('')
  const [keyOpen, setKeyOpen] = useState(false)

  const { data: detail, isLoading } = useQuery({
    queryKey: ['agent', id], queryFn: () => agentsApi.get(id!), enabled: !isNew,
  })
  const optionQueries = useQueries({ queries: [
    { queryKey: ['agent-options', 'models'], queryFn: () => agentsApi.options('models') },
    { queryKey: ['agent-options', 'sandbox-profiles'], queryFn: () => agentsApi.options('sandbox-profiles') },
    { queryKey: ['agent-options', 'platform-tools'], queryFn: () => agentsApi.options('platform-tools') },
    { queryKey: ['agent-options', 'skills'], queryFn: () => agentsApi.options('skills') },
    { queryKey: ['agent-options', 'agents'], queryFn: () => agentsApi.options('agents') },
    ...policyTypes.map(type => ({ queryKey: ['agent-options', 'policies', type], queryFn: () => agentsApi.options('policy-definitions', { policy_type: type }) })),
  ] })
  const [models, sandboxes, tools, skills, boundAgents, ...policies] = optionQueries.map(q => q.data ?? [])
  const options = (items: AgentOption[]) => items.map(item => ({ label: item.key ? `${item.name} · ${item.key}` : item.name, value: item.id }))

  useEffect(() => {
    if (detail) form.setFieldsValue({ ...detail.draft, visibility: detail.agent.visibility })
  }, [detail, form])

  const save = useMutation({
    mutationFn: async (values: Partial<AgentDraft> & { visibility?: string }) => {
      const { visibility, ...draft } = values
      if (isNew) return agentsApi.create(draft, visibility)
      return agentsApi.patchDraft(id!, detail!.draft.draft_revision, draft)
    },
    onSuccess: (result: any) => {
      message.success('草稿已保存')
      queryClient.invalidateQueries({ queryKey: ['agents'] })
      if (isNew) navigate(`/agents/${result.agent.id}/config`, { replace: true })
      else {
        queryClient.invalidateQueries({ queryKey: ['agent', id] })
        queryClient.invalidateQueries({ queryKey: ['agent-capability-check', id] })
      }
    },
    onError: (error: any) => message.error(error?.response?.status === 409 ? '草稿已在别处更新，请重新载入' : '保存失败'),
  })
  const publish = useMutation({
    mutationFn: () => agentsApi.publish(id!, detail!.draft.draft_revision),
    onSuccess: () => {
      message.success('已发布不可变版本')
      queryClient.invalidateQueries({ queryKey: ['agent', id] })
      queryClient.invalidateQueries({ queryKey: ['agents'] })
      queryClient.invalidateQueries({ queryKey: ['agent-capability-check', id] })
      queryClient.invalidateQueries({ queryKey: ['agent-versions', id] })
    },
    onError: (error: any) => {
      const issues = error?.response?.data?.error?.details?.issues
      message.error(issues?.join('；') || '发布失败')
    },
  })
  const draftTest = useMutation({
    mutationFn: () => agentsApi.testDraft(id!, detail!.draft.draft_revision, testContent.trim()),
    onSuccess: result => {
      setTestOpen(false)
      setTestContent('')
      message.success('已创建不可变草稿测试快照')
      navigate(`/agents/${id}/chat/${result.message.session_id}`)
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.details?.issues?.join('；') || '草稿测试创建失败'),
  })
  const stateChange = useMutation({
    mutationFn: (target: 'deactivate' | 'reactivate') => agentsApi.setState(
      id!, target, target === 'deactivate' ? 'Builder 从配置页停用' : 'Builder 从配置页重新启用',
    ),
    onSuccess: result => {
      message.success(result.status === 'inactive' ? 'Agent 已停用' : 'Agent 已重新启用')
      queryClient.invalidateQueries({ queryKey: ['agent', id] })
      queryClient.invalidateQueries({ queryKey: ['agents'] })
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || 'Agent 状态更新失败'),
  })
  const apiKeys = useQuery({
    queryKey: ['agent-api-keys', id], queryFn: () => agentsApi.listApiKeys(id!), enabled: !isNew,
  })
  const capabilityCheck = useQuery({
    queryKey: ['agent-capability-check', id], queryFn: () => agentsApi.checkCapabilities(id!), enabled: !isNew,
  })
  const versions = useQuery({
    queryKey: ['agent-versions', id], queryFn: () => agentsApi.listVersions(id!), enabled: !isNew,
  })
  const showSecret = (key: AgentApiKey) => Modal.success({
    title: 'API Key 仅显示一次',
    width: 680,
    content: <div className="space-y-2"><Alert type="warning" showIcon message="关闭后无法再次查看；请立即保存到企业密钥管理系统。" />
      <Typography.Text copyable code>{key.secret}</Typography.Text></div>,
  })
  const createKey = useMutation({
    mutationFn: (values: { name: string; caller_label: string; expires_at?: string }) =>
      agentsApi.createApiKey(id!, { ...values, expires_at: values.expires_at || null }),
    onSuccess: key => {
      setKeyOpen(false); keyForm.resetFields(); showSecret(key)
      queryClient.invalidateQueries({ queryKey: ['agent-api-keys', id] })
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || 'API Key 创建失败'),
  })
  const rotateKey = useMutation({
    mutationFn: (key: AgentApiKey) => agentsApi.rotateApiKey(id!, key),
    onSuccess: key => {
      showSecret(key)
      queryClient.invalidateQueries({ queryKey: ['agent-api-keys', id] })
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || 'API Key 轮换失败'),
  })
  const revokeKey = useMutation({
    mutationFn: (key: AgentApiKey) => agentsApi.revokeApiKey(id!, key.id),
    onSuccess: () => {
      message.success('API Key 已吊销')
      queryClient.invalidateQueries({ queryKey: ['agent-api-keys', id] })
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || 'API Key 吊销失败'),
  })

  if (!isNew && isLoading) return <Spin size="large" className="mx-auto mt-20 block" />
  const draft = detail?.draft

  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-6 flex items-start justify-between gap-4">
        <Space align="start">
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/agents')}>返回</Button>
          <div>
            <Typography.Title level={3} style={{ margin: 0 }}>{isNew ? '装配新 Agent' : draft?.name || '未命名 Agent'}</Typography.Title>
            {!isNew && <Typography.Text type="secondary">草稿 revision {draft?.draft_revision} · 已发布 v{detail?.agent.latest_published_version_no ?? '—'}</Typography.Text>}
          </div>
          {detail && <Tag color={detail.agent.status === 'published' ? 'green' : 'gold'}>{detail.agent.status}</Tag>}
        </Space>
        <Space>
          <Button icon={<SaveOutlined />} loading={save.isPending} onClick={() => form.validateFields().then(values => save.mutate(values))}>保存草稿</Button>
          {!isNew && <Button icon={<ExperimentOutlined />} onClick={() => setTestOpen(true)}>测试草稿</Button>}
          {!isNew && detail?.agent.current_published_version_id && <Button
            danger={detail.agent.status !== 'inactive'}
            icon={detail.agent.status === 'inactive' ? <PlayCircleOutlined /> : <PauseCircleOutlined />}
            loading={stateChange.isPending}
            onClick={() => Modal.confirm({
              title: detail.agent.status === 'inactive' ? '重新启用 Agent？' : '停用 Agent？',
              content: detail.agent.status === 'inactive'
                ? '只影响后续新 Run；已经冻结的 Plan / Group 仍按其版本规则结算。'
                : '将阻止新的 Chat、Plan Proposal 与 Group Formation；已确认执行不会被移除。',
              onOk: () => stateChange.mutateAsync(detail.agent.status === 'inactive' ? 'reactivate' : 'deactivate'),
            })}>{detail.agent.status === 'inactive' ? '重新启用' : '停用'}</Button>}
          {!isNew && <Button type="primary" icon={<CloudUploadOutlined />} loading={publish.isPending} onClick={() => publish.mutate()}>发布版本</Button>}
        </Space>
      </div>

      <Alert className="mb-4" type="info" showIcon message="Chat 新 Run 使用最新发布版本；已确认的 Plan / Group 不会随草稿变化。" />
      {!isNew && capabilityCheck.data && <Alert className="mb-4" showIcon
        type={capabilityCheck.data.publishable ? 'success' : 'error'}
        message={capabilityCheck.data.publishable ? '当前已保存草稿通过发布前能力检查' : '当前已保存草稿不可发布'}
        description={capabilityCheck.data.publishable
          ? '发布时会冻结精确 Model、Tool 注册哈希、SkillVersion/checksum、Policy、Sandbox 与 AgentCard。'
          : capabilityCheck.data.issues.join('；')} />}
      <Form form={form} layout="vertical" initialValues={{ visibility: 'private', group_enabled: false, tool_ids: [], skill_ids: [], bound_agent_ids: [] }}>
        <Tabs tabPosition="left" items={[
          { key: 'basic', label: '基础信息', children: <Card>
            <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input maxLength={200} /></Form.Item>
            <Form.Item name="description" label="任务描述" rules={[{ required: true }]}><Input.TextArea rows={4} /></Form.Item>
            <Form.Item name="visibility" label="可见范围"><Radio.Group disabled={!isNew} options={[{ label: '私有', value: 'private' }, { label: '企业', value: 'enterprise' }]} /></Form.Item>
          </Card> },
          { key: 'model', label: '模型与指令', children: <Card>
            <Form.Item name="model_id" label="模型" rules={[{ required: true }]}><Select options={options(models)} placeholder="选择平台模型" /></Form.Item>
            <Form.Item name="sandbox_profile_id" label="Sandbox" rules={[{ required: true }]}><Select options={options(sandboxes)} /></Form.Item>
            <Form.Item name="instructions" label="Agent Instructions" rules={[{ required: true }]}><PromptEditor height={320} /></Form.Item>
            <Typography.Text type="secondary">采样参数由平台 Model Registry 统一治理。</Typography.Text>
          </Card> },
          { key: 'capabilities', label: '能力装配', children: <Card>
            <Form.Item name="tool_ids" label="Platform Tools"><Select mode="multiple" options={options(tools)} optionFilterProp="label" /></Form.Item>
            <Form.Item name="skill_ids" label="Skills"><Select mode="multiple" options={options(skills)} optionFilterProp="label" /></Form.Item>
          </Card> },
          { key: 'context', label: '上下文与记忆', children: <Card>
            <Form.Item name="context_policy_id" label="Context Policy" rules={[{ required: true }]}><Select options={options(policies[0])} /></Form.Item>
            <Form.Item name="history_policy_id" label="History Policy" rules={[{ required: true }]}><Select options={options(policies[1])} /></Form.Item>
            <Form.Item name="memory_write_policy_id" label="Memory Write Policy" rules={[{ required: true }]}><Select options={options(policies[2])} /></Form.Item>
            <Form.Item name="memory_recall_policy_id" label="Memory Recall Policy" rules={[{ required: true }]}><Select options={options(policies[3])} /></Form.Item>
          </Card> },
          { key: 'collaboration', label: '协作与访问', children: <Card>
            <Form.Item name="bound_agent_ids" label="可调用 Agent"><Select mode="multiple" options={options(boundAgents.filter(item => item.id !== id))} optionFilterProp="label" /></Form.Item>
            <Form.Item name="group_enabled" label="允许加入 Group" valuePropName="checked"><Switch /></Form.Item>
            <Typography.Text type="secondary">绑定只授予发现与调用范围，不提升原始用户权限。</Typography.Text>
          </Card> },
          ...(!isNew ? [{ key: 'versions', label: '版本历史', children: <Card>
            <Table rowKey="id" loading={versions.isLoading} dataSource={versions.data ?? []} pagination={false}
              expandable={{ expandedRowRender: (version: AgentPublishedVersion) => <div className="space-y-2">
                <Typography.Paragraph copyable={{ text: version.content_hash }}><Typography.Text strong>完整快照哈希：</Typography.Text> {version.content_hash}</Typography.Paragraph>
                <Typography.Paragraph copyable={{ text: version.model_version_id }}><Typography.Text strong>ModelVersion：</Typography.Text> {version.model_version_id}</Typography.Paragraph>
                <Typography.Paragraph><Typography.Text strong>PolicyVersions：</Typography.Text> {[version.context_policy_version_id, version.history_policy_version_id, version.memory_write_policy_version_id, version.memory_recall_policy_version_id].join(' · ')}</Typography.Paragraph>
                <Typography.Paragraph><Typography.Text strong>SandboxProfileVersion：</Typography.Text> {version.sandbox_profile_version_id}</Typography.Paragraph>
                <Typography.Paragraph><Typography.Text strong>Tools：</Typography.Text> {version.tools.map(tool => `${tool.tool_id}@${tool.registration_hash.slice(0, 12)}`).join(' · ') || '无'}</Typography.Paragraph>
                <Typography.Paragraph><Typography.Text strong>Skills：</Typography.Text> {version.skills.map(skill => `${skill.skill_version_id}@${skill.checksum.slice(0, 12)}`).join(' · ') || '无'}</Typography.Paragraph>
                <Typography.Paragraph copyable={{ text: version.agent_card_content_hash || '' }}><Typography.Text strong>AgentCard：</Typography.Text> {version.agent_card_content_hash || '—'}</Typography.Paragraph>
              </div> }}
              columns={[
                { title: '版本', dataIndex: 'version_no', render: (value: number) => `v${value}` },
                { title: '状态', dataIndex: 'publication_status', render: (value: string) => <Tag color={value === 'published' ? 'success' : 'default'}>{value}</Tag> },
                { title: '快照哈希', dataIndex: 'content_hash', render: (value: string) => <Typography.Text code copyable>{value.slice(0, 16)}</Typography.Text> },
                { title: '发布时间', dataIndex: 'published_at', render: (value: string | null) => value ? new Date(value).toLocaleString() : '—' },
              ]} />
          </Card> }] : []),
          ...(!isNew ? [{ key: 'api', label: 'API 接入', children: <Card title="Agent API Keys"
            extra={<Button type="primary" icon={<KeyOutlined />} onClick={() => setKeyOpen(true)}>创建 Key</Button>}>
            <Alert className="mb-4" type="info" showIcon message="外部系统使用 AgentApiKey + userId 调用；Key 不代表新的用户身份。" />
            <Table rowKey="id" loading={apiKeys.isLoading} dataSource={apiKeys.data ?? []} pagination={false} columns={[
              { title: '名称', dataIndex: 'name' },
              { title: '调用方', dataIndex: 'caller_label' },
              { title: 'Prefix', dataIndex: 'prefix', render: (value: string) => <Typography.Text code>{value}</Typography.Text> },
              { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={value === 'active' ? 'success' : 'default'}>{value}</Tag> },
              { title: '到期', dataIndex: 'expires_at', render: (value: string | null) => value ? new Date(value).toLocaleString() : '永不' },
              { title: '最后使用', dataIndex: 'last_used_at', render: (value: string | null) => value ? new Date(value).toLocaleString() : '—' },
              { title: '操作', render: (_: unknown, key: AgentApiKey) => <Space>
                <Button size="small" disabled={key.status !== 'active'} loading={rotateKey.isPending} onClick={() => Modal.confirm({
                  title: '轮换 API Key？', content: '新 Key 会立即生效；旧 Key 将被吊销。', onOk: () => rotateKey.mutateAsync(key),
                })}>轮换</Button>
                <Button size="small" danger disabled={key.status !== 'active'} loading={revokeKey.isPending} onClick={() => Modal.confirm({
                  title: '吊销 API Key？', content: '吊销立即生效且不可撤销。', okButtonProps: { danger: true }, onOk: () => revokeKey.mutateAsync(key),
                })}>吊销</Button>
              </Space> },
            ]} />
          </Card> }] : []),
        ]} />
      </Form>
      <Modal title="测试当前草稿" open={testOpen} onCancel={() => setTestOpen(false)}
        okText="创建测试 Run" confirmLoading={draftTest.isPending}
        okButtonProps={{ disabled: !testContent.trim() }} onOk={() => draftTest.mutate()}>
        <Alert className="mb-3" type="warning" showIcon
          message="平台会复制当前 revision 为不可变测试快照"
          description="后续草稿编辑不会改变本次测试；该 Run 为私有 Chat，不能进入 Plan / Group，也不能被其他 Agent 调用。" />
        <Input.TextArea value={testContent} onChange={event => setTestContent(event.target.value)}
          placeholder="输入草稿测试消息" autoSize={{ minRows: 3, maxRows: 8 }} />
      </Modal>
      <Modal title="创建 Agent API Key" open={keyOpen} onCancel={() => setKeyOpen(false)}
        onOk={() => keyForm.submit()} confirmLoading={createKey.isPending}>
        <Form form={keyForm} layout="vertical" onFinish={values => createKey.mutate(values)}>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="caller_label" label="调用方标识" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="expires_at" label="到期时间（ISO 8601，可选）"><Input placeholder="2026-12-31T23:59:59+08:00" /></Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
