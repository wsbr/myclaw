import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Form, Input, InputNumber, Modal, Radio, Select, Space, Spin, Switch, Table, Tabs, Tag, Typography, message } from 'antd'
import { ArrowLeftOutlined, CloudUploadOutlined, ExperimentOutlined, KeyOutlined, PauseCircleOutlined, PlayCircleOutlined, PlusOutlined, SaveOutlined, UserAddOutlined } from '@ant-design/icons'
import { agentsApi, AgentApiKey, AgentDraft, AgentEvalRun, AgentEvalSuite, AgentOption, AgentPublishedVersion } from '@/api/agents'
import PromptEditor from '@/components/PromptEditor'
import { displayLabel } from '@/lib/presentation'

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
  const [evalOpen, setEvalOpen] = useState(false)
  const [editingSuite, setEditingSuite] = useState<AgentEvalSuite | null>(null)
  const [evalForm] = Form.useForm()
  const [memberIds, setMemberIds] = useState<string[]>([])

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
      id!, target, target === 'deactivate' ? '构建者从配置页停用' : '构建者从配置页重新启用',
    ),
    onSuccess: result => {
      message.success(result.status === 'inactive' ? '智能体已停用' : '智能体已重新启用')
      queryClient.invalidateQueries({ queryKey: ['agent', id] })
      queryClient.invalidateQueries({ queryKey: ['agents'] })
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || '智能体状态更新失败'),
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
  const evalSuites = useQuery({
    queryKey: ['agent-eval-suites', id], queryFn: () => agentsApi.listEvalSuites(id!),
    enabled: !isNew,
  })
  const evalRuns = useQuery({
    queryKey: ['agent-eval-runs', id], queryFn: () => agentsApi.listEvalRuns(id!),
    enabled: !isNew,
    refetchInterval: query => query.state.data?.some(run => ['queued', 'running'].includes(run.status)) ? 3000 : false,
  })
  const agentMembers = useQuery({
    queryKey: ['agent-members', id], queryFn: () => agentsApi.getMembers(id!),
    enabled: !isNew && detail?.agent.visibility === 'private',
  })
  useEffect(() => {
    if (agentMembers.data) {
      setMemberIds(agentMembers.data.members.map(item => item.account_id))
    }
  }, [agentMembers.data])
  const saveMembers = useMutation({
    mutationFn: () => agentsApi.replaceMembers(
      id!, agentMembers.data!.agent_version, memberIds,
    ),
    onSuccess: result => {
      setMemberIds(result.members.map(item => item.account_id))
      message.success('成员白名单已更新')
      queryClient.invalidateQueries({ queryKey: ['agent-members', id] })
      queryClient.invalidateQueries({ queryKey: ['agent', id] })
      queryClient.invalidateQueries({ queryKey: ['agents'] })
    },
    onError: (error: any) => {
      if (error?.response?.status === 409) {
        queryClient.invalidateQueries({ queryKey: ['agent-members', id] })
        queryClient.invalidateQueries({ queryKey: ['agent', id] })
        message.error('智能体访问配置已更新，请确认后重新保存')
      } else {
        message.error(error?.response?.data?.error?.message || '成员白名单保存失败')
      }
    },
  })
  const openEvalDefinition = (suite?: AgentEvalSuite) => {
    const selected = suite ?? null
    setEditingSuite(selected)
    evalForm.setFieldsValue({
      name: selected?.name ?? '发布质量门禁',
      description: selected?.description ?? '',
      min_pass_rate: Number(selected?.gate_policy?.min_pass_rate ?? 1),
      cases_json: JSON.stringify(selected?.cases.map(item => ({
        case_key: item.case_key, name: item.name, input_text: item.input_text,
        assertions: item.assertions, weight: item.weight,
      })) ?? [{
        case_key: 'basic', name: '基础结果', input_text: '请完成一项典型任务',
        assertions: { min_chars: 1 }, weight: 1,
      }], null, 2),
    })
    setEvalOpen(true)
  }
  const saveEvalSuite = useMutation({
    mutationFn: async (values: { name: string; description?: string; min_pass_rate: number; cases_json: string }) => {
      let cases
      try { cases = JSON.parse(values.cases_json) } catch { throw new Error('cases_json_invalid') }
      if (!Array.isArray(cases)) throw new Error('cases_json_invalid')
      const definition = {
        name: values.name, description: values.description ?? '',
        gate_policy: { min_pass_rate: String(values.min_pass_rate) }, cases,
      }
      return editingSuite
        ? agentsApi.versionEvalSuite(id!, editingSuite.id, definition)
        : agentsApi.createEvalSuite(id!, definition)
    },
    onSuccess: () => {
      setEvalOpen(false); setEditingSuite(null); evalForm.resetFields()
      message.success(editingSuite ? '已发布新的不可变评测套件版本' : '已创建发布门禁套件')
      queryClient.invalidateQueries({ queryKey: ['agent-eval-suites', id] })
      queryClient.invalidateQueries({ queryKey: ['agent-capability-check', id] })
    },
    onError: (error: any) => message.error(
      error?.message === 'cases_json_invalid'
        ? '用例 JSON 必须是数组'
        : error?.response?.data?.error?.message || '评测套件保存失败',
    ),
  })
  const runEval = useMutation({
    mutationFn: (suite: AgentEvalSuite) => agentsApi.runEvalSuite(
      id!, suite.id, detail!.draft.draft_revision,
    ),
    onSuccess: () => {
      message.success('评测已启动，每个用例都创建了独立的真实运行')
      queryClient.invalidateQueries({ queryKey: ['agent-eval-runs', id] })
    },
    onError: (error: any) => message.error(
      error?.response?.data?.error?.details?.issues?.join('；')
      || error?.response?.data?.error?.message || '评测启动失败',
    ),
  })
  const evalState = useMutation({
    mutationFn: (suite: AgentEvalSuite) => agentsApi.setEvalSuiteState(
      id!, suite.id, suite.status === 'active' ? 'deactivate' : 'activate',
    ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agent-eval-suites', id] })
      queryClient.invalidateQueries({ queryKey: ['agent-capability-check', id] })
    },
    onError: () => message.error('评测门禁状态更新失败'),
  })
  const showSecret = (key: AgentApiKey) => Modal.success({
    title: '访问密钥仅显示一次',
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
    onError: (error: any) => message.error(error?.response?.data?.error?.code || '访问密钥创建失败'),
  })
  const rotateKey = useMutation({
    mutationFn: (key: AgentApiKey) => agentsApi.rotateApiKey(id!, key),
    onSuccess: key => {
      showSecret(key)
      queryClient.invalidateQueries({ queryKey: ['agent-api-keys', id] })
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || '访问密钥轮换失败'),
  })
  const revokeKey = useMutation({
    mutationFn: (key: AgentApiKey) => agentsApi.revokeApiKey(id!, key.id),
    onSuccess: () => {
      message.success('访问密钥已吊销')
      queryClient.invalidateQueries({ queryKey: ['agent-api-keys', id] })
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || '访问密钥吊销失败'),
  })

  if (!isNew && isLoading) return <Spin size="large" className="mx-auto mt-20 block" />
  const draft = detail?.draft

  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-6 flex items-start justify-between gap-4">
        <Space align="start">
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/agents')}>返回</Button>
          <div>
            <Typography.Title level={3} style={{ margin: 0 }}>{isNew ? '装配新智能体' : draft?.name || '未命名智能体'}</Typography.Title>
            {!isNew && <Typography.Text type="secondary">草稿修订 {draft?.draft_revision} · 已发布 v{detail?.agent.latest_published_version_no ?? '—'}</Typography.Text>}
          </div>
          {detail && <Tag color={detail.agent.status === 'published' ? 'green' : 'gold'}>{displayLabel(detail.agent.status)}</Tag>}
        </Space>
        <Space>
          <Button icon={<SaveOutlined />} loading={save.isPending} onClick={() => form.validateFields().then(values => save.mutate(values))}>保存草稿</Button>
          {!isNew && <Button icon={<ExperimentOutlined />} onClick={() => setTestOpen(true)}>测试草稿</Button>}
          {!isNew && detail?.agent.current_published_version_id && <Button
            danger={detail.agent.status !== 'inactive'}
            icon={detail.agent.status === 'inactive' ? <PlayCircleOutlined /> : <PauseCircleOutlined />}
            loading={stateChange.isPending}
            onClick={() => Modal.confirm({
              title: detail.agent.status === 'inactive' ? '重新启用智能体？' : '停用智能体？',
              content: detail.agent.status === 'inactive'
                ? '只影响后续新运行；已经冻结的规划与群组仍按其版本规则结算。'
                : '将阻止新的对话、规划与群组执行；已固定的运行快照不会被移除。',
              onOk: () => stateChange.mutateAsync(detail.agent.status === 'inactive' ? 'reactivate' : 'deactivate'),
            })}>{detail.agent.status === 'inactive' ? '重新启用' : '停用'}</Button>}
          {!isNew && <Button type="primary" icon={<CloudUploadOutlined />} loading={publish.isPending} onClick={() => publish.mutate()}>发布版本</Button>}
        </Space>
      </div>

      <Alert className="mb-4" type="info" showIcon message="新对话使用最新发布版本；已确认的规划与群组不会随草稿变化。" />
      {!isNew && capabilityCheck.data && <Alert className="mb-4" showIcon
        type={capabilityCheck.data.publishable ? 'success' : 'error'}
        message={capabilityCheck.data.publishable ? '当前已保存草稿通过发布前能力检查' : '当前已保存草稿不可发布'}
        description={capabilityCheck.data.publishable
          ? '发布时会冻结精确模型、工具注册哈希、技能版本与校验和、策略、沙箱和智能体卡片。'
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
            <Form.Item name="sandbox_profile_id" label="沙箱配置" rules={[{ required: true }]}><Select options={options(sandboxes)} /></Form.Item>
            <Form.Item name="instructions" label="智能体指令" rules={[{ required: true }]}><PromptEditor height={320} /></Form.Item>
            <Typography.Text type="secondary">采样参数由平台模型注册表统一治理。</Typography.Text>
          </Card> },
          { key: 'capabilities', label: '能力装配', children: <Card>
            <Form.Item name="tool_ids" label="平台工具" extra="按智能体显式装配；例如选中“定时任务”后，用户即可在普通对话中用自然语言创建和管理任务。"><Select mode="multiple" options={options(tools)} optionFilterProp="label" /></Form.Item>
            <Form.Item name="skill_ids" label="技能"><Select mode="multiple" options={options(skills)} optionFilterProp="label" /></Form.Item>
          </Card> },
          { key: 'context', label: '上下文与记忆', children: <Card>
            <Form.Item name="context_policy_id" label="上下文策略" rules={[{ required: true }]}><Select options={options(policies[0])} /></Form.Item>
            <Form.Item name="history_policy_id" label="历史策略" rules={[{ required: true }]}><Select options={options(policies[1])} /></Form.Item>
            <Form.Item name="memory_write_policy_id" label="记忆写入策略" rules={[{ required: true }]}><Select options={options(policies[2])} /></Form.Item>
            <Form.Item name="memory_recall_policy_id" label="记忆召回策略" rules={[{ required: true }]}><Select options={options(policies[3])} /></Form.Item>
          </Card> },
          { key: 'collaboration', label: '协作与访问', children: <Card>
            <Form.Item name="bound_agent_ids" label="可调用智能体"><Select mode="multiple" options={options(boundAgents.filter(item => item.id !== id))} optionFilterProp="label" /></Form.Item>
            <Form.Item name="group_enabled" label="允许加入群组协作" valuePropName="checked"><Switch /></Form.Item>
            <Typography.Paragraph type="secondary">绑定只授予发现与调用范围，不提升原始用户权限。</Typography.Paragraph>
            {isNew ? <Alert showIcon type="info" message="创建私有智能体后即可配置成员白名单" />
              : detail?.agent.visibility === 'private' ? <Card size="small" title="私有智能体成员白名单">
                <Alert className="mb-4" showIcon type="info"
                  message="白名单成员只能发现和使用这个智能体"
                  description="成员可以在智能体列表中看到它、发起对话，并在对话中使用已装配能力；不会获得草稿配置、测试、发布、评测、访问密钥或成员管理权限。移出后历史对话仍可查看，但不能再发送新消息。" />
                <Space direction="vertical" className="w-full">
                  <Select
                    mode="multiple"
                    allowClear
                    showSearch
                    optionFilterProp="label"
                    loading={agentMembers.isLoading}
                    value={memberIds}
                    onChange={setMemberIds}
                    placeholder="选择可使用此智能体的 Tenant 成员"
                    options={(agentMembers.data?.candidates ?? []).map(account => ({
                      value: account.account_id,
                      label: `${account.display_name} · ${account.email || account.user_id}`,
                    }))}
                    className="w-full"
                  />
                  <Button
                    icon={<UserAddOutlined />}
                    loading={saveMembers.isPending}
                    disabled={!agentMembers.data}
                    onClick={() => saveMembers.mutate()}
                  >保存成员白名单</Button>
                </Space>
              </Card> : <Alert showIcon type="success"
                message="企业智能体已向 Tenant 全体有效成员开放"
                description="无需维护单独白名单；成员只有使用权限，管理权限仍属于 Owner。" />}
          </Card> },
          ...(!isNew ? [{ key: 'versions', label: '版本历史', children: <Card>
            <Table rowKey="id" loading={versions.isLoading} dataSource={versions.data ?? []} pagination={false}
              expandable={{ expandedRowRender: (version: AgentPublishedVersion) => <div className="space-y-2">
                <Typography.Paragraph copyable={{ text: version.content_hash }}><Typography.Text strong>完整快照哈希：</Typography.Text> {version.content_hash}</Typography.Paragraph>
                <Typography.Paragraph copyable={{ text: version.model_version_id }}><Typography.Text strong>模型版本：</Typography.Text> {version.model_version_id}</Typography.Paragraph>
                <Typography.Paragraph><Typography.Text strong>策略版本：</Typography.Text> {[version.context_policy_version_id, version.history_policy_version_id, version.memory_write_policy_version_id, version.memory_recall_policy_version_id].join(' · ')}</Typography.Paragraph>
                <Typography.Paragraph><Typography.Text strong>沙箱配置版本：</Typography.Text> {version.sandbox_profile_version_id}</Typography.Paragraph>
                <Typography.Paragraph><Typography.Text strong>工具：</Typography.Text> {version.tools.map(tool => `${tool.tool_id}@${tool.registration_hash.slice(0, 12)}`).join(' · ') || '无'}</Typography.Paragraph>
                <Typography.Paragraph><Typography.Text strong>技能：</Typography.Text> {version.skills.map(skill => `${skill.skill_version_id}@${skill.checksum.slice(0, 12)}`).join(' · ') || '无'}</Typography.Paragraph>
                <Typography.Paragraph copyable={{ text: version.agent_card_content_hash || '' }}><Typography.Text strong>智能体卡片：</Typography.Text> {version.agent_card_content_hash || '—'}</Typography.Paragraph>
              </div> }}
              columns={[
                { title: '版本', dataIndex: 'version_no', render: (value: number) => `v${value}` },
                { title: '状态', dataIndex: 'publication_status', render: (value: string) => <Tag color={value === 'published' ? 'success' : 'default'}>{displayLabel(value)}</Tag> },
                { title: '快照哈希', dataIndex: 'content_hash', render: (value: string) => <Typography.Text code copyable>{value.slice(0, 16)}</Typography.Text> },
                { title: '发布时间', dataIndex: 'published_at', render: (value: string | null) => value ? new Date(value).toLocaleString() : '—' },
              ]} />
          </Card> }] : []),
          ...(!isNew ? [{ key: 'evals', label: '评测与门禁', children: <Space direction="vertical" size="large" className="w-full">
            <Alert showIcon type="info" message="评测证据与发布候选精确绑定"
              description="每个用例创建独立的真实草稿测试运行。只有当前套件版本通过，且草稿与冻结依赖的候选哈希完全一致，发布按钮才会放行；任何草稿或依赖变化都会使旧成绩失效。" />
            <Card title="发布门禁套件" extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => openEvalDefinition()}>新建套件</Button>}>
              <Table rowKey="id" loading={evalSuites.isLoading} dataSource={evalSuites.data ?? []} pagination={false}
                expandable={{ expandedRowRender: (suite: AgentEvalSuite) => <div className="space-y-2">
                  <Typography.Paragraph>{suite.description || '无说明'}</Typography.Paragraph>
                  <Typography.Paragraph copyable={{ text: suite.content_hash }}><Typography.Text strong>套件哈希：</Typography.Text> {suite.content_hash}</Typography.Paragraph>
                  <Table rowKey="id" size="small" pagination={false} dataSource={suite.cases} columns={[
                    { title: '用例', dataIndex: 'name' },
                    { title: '用例键', dataIndex: 'case_key' },
                    { title: '输入', dataIndex: 'input_text', ellipsis: true },
                    { title: '断言', dataIndex: 'assertions', render: (value: object) => <Typography.Text code>{JSON.stringify(value)}</Typography.Text> },
                  ]} />
                </div> }}
                columns={[
                  { title: '名称', dataIndex: 'name' },
                  { title: '版本', dataIndex: 'current_version_no', render: (value: number) => `v${value}` },
                  { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={value === 'active' ? 'success' : 'default'}>{displayLabel(value)}</Tag> },
                  { title: '通过线', dataIndex: 'gate_policy', render: (value: { min_pass_rate?: string }) => `${Number(value?.min_pass_rate ?? 1) * 100}%` },
                  { title: '用例数', dataIndex: 'cases', render: (value: unknown[]) => value.length },
                  { title: '操作', render: (_: unknown, suite: AgentEvalSuite) => <Space wrap>
                    <Button size="small" onClick={() => openEvalDefinition(suite)}>发布新版本</Button>
                    <Button size="small" type="primary" disabled={suite.status !== 'active'} loading={runEval.isPending} onClick={() => runEval.mutate(suite)}>运行评测</Button>
                    <Button size="small" loading={evalState.isPending} onClick={() => evalState.mutate(suite)}>{suite.status === 'active' ? '停用门禁' : '启用门禁'}</Button>
                  </Space> },
                ]} />
            </Card>
            <Card title="评测运行">
              <Table rowKey="id" loading={evalRuns.isLoading} dataSource={evalRuns.data ?? []} pagination={false}
                expandable={{ expandedRowRender: (run: AgentEvalRun) => <Table rowKey="id" size="small" pagination={false} dataSource={run.case_runs} columns={[
                  { title: '用例', dataIndex: 'name' },
                  { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={value === 'passed' ? 'success' : value === 'failed' || value === 'error' ? 'error' : 'processing'}>{displayLabel(value)}</Tag> },
                  { title: '断言结果', dataIndex: 'assertion_results', render: (value: object[]) => value.length ? <Typography.Text code>{JSON.stringify(value)}</Typography.Text> : '等待结算' },
                  { title: '错误', dataIndex: 'failure_code', render: (value: string | null) => value || '—' },
                  { title: '运行', render: (_: unknown, item: { session_id: string; run_id: string }) => <Button size="small" onClick={() => navigate(`/agents/${id}/chat/${item.session_id}`)}>查看过程</Button> },
                ]} /> }}
                columns={[
                  { title: '套件', render: (_: unknown, run: AgentEvalRun) => `${run.suite_name} v${run.suite_version_no}` },
                  { title: '候选哈希', dataIndex: 'candidate_content_hash', render: (value: string) => <Typography.Text code copyable>{value.slice(0, 16)}</Typography.Text> },
                  { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={value === 'passed' ? 'success' : value === 'failed' || value === 'error' ? 'error' : 'processing'}>{displayLabel(value)}</Tag> },
                  { title: '结果', render: (_: unknown, run: AgentEvalRun) => `${run.passed_count}/${run.case_count} · ${run.score == null ? '—' : `${(Number(run.score) * 100).toFixed(1)}%`}` },
                  { title: '时间', dataIndex: 'created_at', render: (value: string) => new Date(value).toLocaleString() },
                ]} />
            </Card>
          </Space> }] : []),
          ...(!isNew ? [{ key: 'api', label: '接口接入', children: <Card title="智能体访问密钥"
            extra={<Button type="primary" icon={<KeyOutlined />} onClick={() => setKeyOpen(true)}>创建密钥</Button>}>
            <Alert className="mb-4" type="info" showIcon message="外部系统使用智能体访问密钥与用户标识调用；密钥不代表新的用户身份。" />
            <Table rowKey="id" loading={apiKeys.isLoading} dataSource={apiKeys.data ?? []} pagination={false} columns={[
              { title: '名称', dataIndex: 'name' },
              { title: '调用方', dataIndex: 'caller_label' },
              { title: '前缀', dataIndex: 'prefix', render: (value: string) => <Typography.Text code>{value}</Typography.Text> },
              { title: '状态', dataIndex: 'status', render: (value: string) => <Tag color={value === 'active' ? 'success' : 'default'}>{displayLabel(value)}</Tag> },
              { title: '到期', dataIndex: 'expires_at', render: (value: string | null) => value ? new Date(value).toLocaleString() : '永不' },
              { title: '最后使用', dataIndex: 'last_used_at', render: (value: string | null) => value ? new Date(value).toLocaleString() : '—' },
              { title: '操作', render: (_: unknown, key: AgentApiKey) => <Space>
                <Button size="small" disabled={key.status !== 'active'} loading={rotateKey.isPending} onClick={() => Modal.confirm({
                  title: '轮换访问密钥？', content: '新密钥会立即生效；旧密钥将被吊销。', onOk: () => rotateKey.mutateAsync(key),
                })}>轮换</Button>
                <Button size="small" danger disabled={key.status !== 'active'} loading={revokeKey.isPending} onClick={() => Modal.confirm({
                  title: '吊销访问密钥？', content: '吊销立即生效且不可撤销。', okButtonProps: { danger: true }, onOk: () => revokeKey.mutateAsync(key),
                })}>吊销</Button>
              </Space> },
            ]} />
          </Card> }] : []),
        ]} />
      </Form>
      <Modal title="测试当前草稿" open={testOpen} onCancel={() => setTestOpen(false)}
        okText="创建测试运行" confirmLoading={draftTest.isPending}
        okButtonProps={{ disabled: !testContent.trim() }} onOk={() => draftTest.mutate()}>
        <Alert className="mb-3" type="warning" showIcon
          message="平台会复制当前 revision 为不可变测试快照"
          description="后续草稿编辑不会改变本次测试；该运行是私有对话，不能进入规划或群组模式，也不能被其他智能体调用。" />
        <Input.TextArea value={testContent} onChange={event => setTestContent(event.target.value)}
          placeholder="输入草稿测试消息" autoSize={{ minRows: 3, maxRows: 8 }} />
      </Modal>
      <Modal title="创建智能体访问密钥" open={keyOpen} onCancel={() => setKeyOpen(false)}
        onOk={() => keyForm.submit()} confirmLoading={createKey.isPending}>
        <Form form={keyForm} layout="vertical" onFinish={values => createKey.mutate(values)}>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="caller_label" label="调用方标识" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="expires_at" label="到期时间（ISO 8601，可选）"><Input placeholder="2026-12-31T23:59:59+08:00" /></Form.Item>
        </Form>
      </Modal>
      <Modal title={editingSuite ? '发布评测套件新版本' : '新建评测门禁套件'} open={evalOpen}
        width={760} onCancel={() => { setEvalOpen(false); setEditingSuite(null) }}
        onOk={() => evalForm.submit()} confirmLoading={saveEvalSuite.isPending}>
        <Alert className="mb-4" type="warning" showIcon message="保存后定义与用例不可修改；编辑会发布新版本，并立即使旧版本成绩不再满足门禁。" />
        <Form form={evalForm} layout="vertical" onFinish={values => saveEvalSuite.mutate(values)}>
          <Form.Item name="name" label="套件名称" rules={[{ required: true }]}><Input maxLength={200} /></Form.Item>
          <Form.Item name="description" label="说明"><Input.TextArea rows={2} /></Form.Item>
          <Form.Item name="min_pass_rate" label="最低加权通过率（0–1）" rules={[{ required: true }]}><InputNumber min={0} max={1} step={0.05} className="w-full" /></Form.Item>
          <Form.Item name="cases_json" label="用例 JSON" rules={[{ required: true }]} extra={'支持 contains_all、contains_none、min_chars、max_chars、max_cost；最多 20 个用例。'}>
            <Input.TextArea autoSize={{ minRows: 12, maxRows: 24 }} style={{ fontFamily: 'monospace' }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
