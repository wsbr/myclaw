import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Form, Input, Modal, Radio, Select, Space, Spin, Switch, Table, Tabs, Tag, Typography, message } from 'antd'
import { ArrowLeftOutlined, CloudUploadOutlined, ExperimentOutlined, KeyOutlined, PauseCircleOutlined, PlayCircleOutlined, SaveOutlined, UserAddOutlined } from '@ant-design/icons'
import { agentsApi, AgentApiKey, AgentDraft, AgentOption, AgentPublishedVersion } from '@/api/agents'
import PromptEditor from '@/components/PromptEditor'
import { displayLabel } from '@/lib/presentation'
import { toolBindingOptions } from '@/lib/toolCatalog'
import { contextPolicyOptions, contextSelectionChanged, contextSelectionUnavailable, matchContextPreset, resolveContextPresets } from './contextPresets'

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
  const contextPolicyId = Form.useWatch('context_policy_id', { form, preserve: true })
  const historyPolicyId = Form.useWatch('history_policy_id', { form, preserve: true })
  const selectedToolIds = Form.useWatch('tool_ids', { form, preserve: true }) as string[] | undefined
  const toolOptions = toolBindingOptions(tools,
    optionQueries[2].isPending || optionQueries[2].isError ? [] : selectedToolIds ?? detail?.draft.tool_ids ?? [])
  const contextPolicyQueries = optionQueries.slice(5, 7)
  const contextOptionsLoading = contextPolicyQueries.some(query => query.isPending)
  const contextOptionsFailed = contextPolicyQueries.some(query => query.isError)
  const contextPresets = resolveContextPresets(policies[0], policies[1])
  const contextSelection = {
    context_policy_id: contextPolicyId, history_policy_id: historyPolicyId,
  }
  const contextPreset = matchContextPreset(contextSelection, policies[0], policies[1])
  const hasUnsavedContextChanges = Boolean(detail && contextSelectionChanged(contextSelection, detail.draft))
  const unavailableSelection = !contextOptionsLoading && !contextOptionsFailed
    && contextSelectionUnavailable(contextSelection, policies[0], policies[1])

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
          {!isNew && <Button icon={<ExperimentOutlined />} disabled={hasUnsavedContextChanges || unavailableSelection || save.isPending} onClick={() => setTestOpen(true)}>测试草稿</Button>}
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
          {!isNew && <Button type="primary" icon={<CloudUploadOutlined />} disabled={hasUnsavedContextChanges || unavailableSelection || save.isPending} loading={publish.isPending} onClick={() => publish.mutate()}>发布版本</Button>}
        </Space>
      </div>

      <Alert className="mb-4" type="info" showIcon message="新对话使用最新发布版本；已确认的规划与群组不会随草稿变化。" />
      {hasUnsavedContextChanges && <Alert className="mb-4" type="info" showIcon
        message="上下文或历史策略已调整，尚未保存"
        description="先保存草稿，再测试或发布；已有发布版本仍使用原来的策略。" />}
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
            {optionQueries[2].isError && <Alert className="mb-4" type="error" showIcon message="工具目录加载失败，现有绑定不会自动更改"
              action={<Button size="small" onClick={() => optionQueries[2].refetch()}>重试</Button>} />}
            <Form.Item name="tool_ids" label="平台工具" extra="文件、检索、定时任务以及会话与协作工具统一在此显式绑定。按需选择，保存草稿并发布后生效；群组专用工具仅在群组中可用。">
              <Select mode="multiple" options={toolOptions} optionFilterProp="label" loading={optionQueries[2].isPending}
                disabled={optionQueries[2].isPending || optionQueries[2].isError} placeholder="选择此智能体需要的工具" />
            </Form.Item>
            <Form.Item name="skill_ids" label="技能"><Select mode="multiple" options={options(skills)} optionFilterProp="label" /></Form.Item>
          </Card> },
          { key: 'context', label: '上下文与记忆', children: <Card>
            <Form.Item label="上下文方案" extra="选择方案只填写当前草稿的上下文与历史策略，保存后才能测试或发布；记忆策略单独配置。">
              <Select
                aria-label="上下文方案"
                value={contextOptionsLoading || contextOptionsFailed ? undefined : contextPreset}
                placeholder={contextOptionsLoading ? '正在加载可用策略…' : '选择方案，或在下方单独配置'}
                loading={contextOptionsLoading}
                disabled={contextOptionsLoading || contextOptionsFailed}
                options={[
                  ...(contextPreset === 'custom' ? [{ value: 'custom', label: '自定义', disabled: true }] : []),
                  ...contextPresets.map(preset => ({
                    value: preset.key,
                    label: preset.selection ? preset.label : `${preset.label}（不可用）`,
                    disabled: !preset.selection,
                    title: preset.unavailableReason || preset.description,
                  })),
                ]}
                onChange={value => {
                  const selection = contextPresets.find(preset => preset.key === value)?.selection
                  if (selection) form.setFieldsValue(selection)
                }}
              />
            </Form.Item>
            {contextOptionsFailed && <Alert className="mb-4" type="error" showIcon
              message="上下文策略加载失败，已保留当前选择"
              action={<Button size="small" onClick={() => contextPolicyQueries.forEach(query => { void query.refetch() })}>重试</Button>} />}
            {unavailableSelection && <Alert className="mb-4" type="warning" showIcon
              message="已选策略缺失或已停用，当前值仍保留"
              description="请重新选择可用方案或策略，并在发布前检查依赖。" />}
            <div className="mb-5 grid gap-3 md:grid-cols-3">
              {contextPresets.map(preset => <div key={preset.key} className="rounded-lg border p-3">
                <Typography.Text strong>{preset.label}</Typography.Text>
                {contextPreset === preset.key && <Tag color="blue" className="ml-2">{hasUnsavedContextChanges ? '待保存' : '草稿方案'}</Tag>}
                <Typography.Paragraph type="secondary" className="!mb-0 mt-2 text-xs">{preset.description}</Typography.Paragraph>
                {!contextOptionsLoading && !contextOptionsFailed && preset.unavailableReason && <Typography.Paragraph type="warning" className="!mb-0 mt-2 text-xs">{preset.unavailableReason}</Typography.Paragraph>}
              </div>)}
            </div>
            <Typography.Paragraph type="secondary">
              方案决定每次请求带入模型的内容，不会删除原始消息或产物；摘要是从可见历史生成的派生信息。
              原文保留不代表每次全部带入模型，摘要和裁剪可能省略细节。
            </Typography.Paragraph>
            <Typography.Paragraph type="secondary">
              方案名称按策略组合识别，不代表使用默认参数。平台可调整策略参数；保存草稿记录策略选择，发布时冻结当时的实际策略版本与参数。
              后续草稿或平台参数修改不改变已发布版本。方案沿用已有访问权限和输入上限。
            </Typography.Paragraph>
            <Typography.Title level={5}>高级策略</Typography.Title>
            <Typography.Paragraph type="secondary">可单独组合下面两项策略；未匹配上述组合时显示“自定义”。</Typography.Paragraph>
            <Form.Item name="context_policy_id" label="上下文策略" rules={[{ required: true }]}><Select options={contextPolicyOptions(policies[0], contextPolicyId)} loading={optionQueries[5].isPending} /></Form.Item>
            <Form.Item name="history_policy_id" label="历史策略" rules={[{ required: true }]}><Select options={contextPolicyOptions(policies[1], historyPolicyId)} loading={optionQueries[6].isPending} /></Form.Item>
            <Typography.Title level={5}>记忆策略</Typography.Title>
            <Typography.Paragraph type="secondary">记忆的写入与召回独立于上下文方案，请按需配置。</Typography.Paragraph>
            <Form.Item name="memory_write_policy_id" label="记忆写入策略" rules={[{ required: true }]}><Select options={options(policies[2])} /></Form.Item>
            <Form.Item name="memory_recall_policy_id" label="记忆召回策略" rules={[{ required: true }]}><Select options={options(policies[3])} /></Form.Item>
          </Card> },
          { key: 'collaboration', label: '协作与访问', children: <Card>
            <Form.Item name="bound_agent_ids" label="显式绑定的可调用智能体" extra="无需开启协作网络。A 绑定 B、B 绑定 C 时可逐跳协作；不会因此允许 A 直接调用 C。"><Select mode="multiple" options={options(boundAgents.filter(item => item.id !== id))} optionFilterProp="label" /></Form.Item>
            <Form.Item name="group_enabled" label="参与协作网络" valuePropName="checked" extra="双方开启后，可在用户已有权限内无需互相绑定进行发现、委派与群组协作。普通对话中，智能体可按需要寻找成员并自动建立包含自己的群组；本轮成员固定，下一轮可调整。"><Switch /></Form.Item>
            <Typography.Paragraph type="secondary">两种协作方式所需的发现、调用与组队工具，都须在「能力装配」中显式绑定并发布。智能体绑定和网络开关均不会提升原始用户权限。</Typography.Paragraph>
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
          ...(!isNew ? [{ key: 'evals', label: '评测与门禁', children: <Card title="评测工作台">
            <Typography.Paragraph>在评测工作台管理用例、导入真实运行、执行回归并对比结果。发布仍会校验当前配置是否满足已启用的门禁。</Typography.Paragraph>
            <Button onClick={() => navigate(`/evals?agent=${id}`)}>打开评测工作台</Button>
          </Card> }] : []),
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
        okButtonProps={{ disabled: !testContent.trim() || hasUnsavedContextChanges || unavailableSelection || save.isPending }} onOk={() => draftTest.mutate()}>
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
    </div>
  )
}
