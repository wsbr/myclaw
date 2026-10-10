import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Empty, Form, Modal, Progress, Select, Space, Statistic, Table, Tabs, Tag, Typography, message } from 'antd'
import { agentsApi } from '@/api/agents'
import type { AgentEvalCaseDefinition, AgentEvalRun, AgentEvalSuite } from '@/api/agents'
import { evalsApi } from '@/api/evals'
import { useAuthStore } from '@/store/auth'
import { displayLabel } from '@/lib/presentation'
import { downloadEvalJson, parseEvalDefinition } from '@/lib/evalDefinitions'
import SuiteEditor from './SuiteEditor'

const terminal = (run: AgentEvalRun) => ['passed', 'failed', 'error'].includes(run.status)
const pct = (value: string | number | null) => value == null ? '—' : `${(Number(value) * 100).toFixed(1)}%`
const errorText = (error: any) => error?.response?.data?.error?.details?.issues?.join('；')
  || error?.response?.data?.error?.details?.message || error?.response?.data?.error?.message
  || (typeof error?.response?.data?.detail === 'string' ? error.response.data.detail : null)
  || error?.message || '操作失败'
const status = (value: string) => <Tag color={value === 'passed' ? 'success' : ['failed', 'error'].includes(value) ? 'error' : 'default'}>{displayLabel(value)}</Tag>
const assertionLabels: Record<string, string> = {
  contains: '必须包含', not_contains: '不得包含', min_chars: '最少字符', max_chars: '最多字符',
  require_tools: '必须调用', require_successful_tools: '必须成功', forbid_tools: '禁止调用', max_cost: '成本上限',
}

function Checks({ items }: { items: Array<Record<string, unknown>> }) {
  return <Space direction="vertical" className="w-full">{items.map((item, index) => <div key={index}>
    <Tag color={item.passed ? 'success' : 'error'}>{item.passed ? '通过' : '失败'}</Tag>
    <Typography.Text>{assertionLabels[String(item.assertion)] || String(item.assertion)}：{String(item.expected ?? item.maximum ?? '')}</Typography.Text>
    {item.actual != null && <Typography.Text type="secondary"> · 实际 {Array.isArray(item.actual) ? item.actual.join('、') || '无' : String(item.actual)}</Typography.Text>}
    {item.unknown_entry_count != null && <Typography.Text type="secondary"> · 未知成本条目 {String(item.unknown_entry_count)} · 不同币种条目 {String(item.unconvertible_entry_count)}</Typography.Text>}
  </div>)}</Space>
}

function Workbench({ agentId }: { agentId: string }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [form] = Form.useForm()
  const [editorOpen, setEditorOpen] = useState(false)
  const [editing, setEditing] = useState<AgentEvalSuite | null>(null)
  const [baseline, setBaseline] = useState<string>()
  const [candidate, setCandidate] = useState<string>()
  const [activeTab, setActiveTab] = useState('suites')
  const [tagFilter, setTagFilter] = useState<string>()
  const detail = useQuery({ queryKey: ['agent', agentId], queryFn: () => agentsApi.get(agentId) })
  const suites = useQuery({ queryKey: ['agent-eval-suites', agentId], queryFn: () => agentsApi.listEvalSuites(agentId) })
  const runs = useQuery({ queryKey: ['agent-eval-runs', agentId], queryFn: () => agentsApi.listEvalRuns(agentId),
    refetchInterval: query => query.state.data?.some(run => !terminal(run)) ? 3000 : false })
  const comparison = useQuery({ queryKey: ['eval-comparison', agentId, baseline, candidate],
    queryFn: () => evalsApi.compare(agentId, baseline!, candidate!), enabled: Boolean(baseline && candidate) })
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['agent-eval-suites', agentId] })
    queryClient.invalidateQueries({ queryKey: ['agent-capability-check', agentId] })
  }
  const openEditor = (suite?: AgentEvalSuite, definition?: any) => {
    const value = definition || suite
    setEditing(suite || null)
    form.resetFields()
    form.setFieldsValue({ name: value?.name || '', description: value?.description || '',
      min_pass_rate: Number(value?.gate_policy?.min_pass_rate ?? 1),
      required_for_publish: value?.gate_policy?.required_for_publish ?? Boolean(suite),
      cases: value?.cases || [],
    })
    setEditorOpen(true)
  }
  const save = useMutation({
    mutationFn: (values: any) => {
      const cases = values.cases.map((item: AgentEvalCaseDefinition) => {
        const assertions = Object.fromEntries(Object.entries(item.assertions || {}).filter(([, value]) => value != null && (!Array.isArray(value) || value.length)))
        const cost = assertions.max_cost as Record<string, unknown> | undefined
        if (cost && !cost.currency && cost.amount == null) delete assertions.max_cost
        return { ...item, assertions }
      })
      const definition = { name: values.name, description: values.description || '',
        expected_version: editing?.version,
        gate_policy: { min_pass_rate: String(values.min_pass_rate), required_for_publish: Boolean(values.required_for_publish) }, cases }
      return editing ? agentsApi.versionEvalSuite(agentId, editing.id, definition) : agentsApi.createEvalSuite(agentId, definition)
    },
    onSuccess: () => { setEditorOpen(false); invalidate(); message.success('评测集版本已保存') },
    onError: error => message.error(errorText(error)),
  })
  const run = useMutation({
    mutationFn: (suite: AgentEvalSuite) => agentsApi.runEvalSuite(agentId, suite.id, detail.data!.draft.draft_revision),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['agent-eval-runs', agentId] }); setActiveTab('runs'); message.success('已创建评测运行') },
    onError: error => { queryClient.invalidateQueries({ queryKey: ['agent', agentId] }); message.error(errorText(error)) },
  })
  const state = useMutation({
    mutationFn: (suite: AgentEvalSuite) => agentsApi.setEvalSuiteState(agentId, suite.id, suite.status === 'active' ? 'deactivate' : 'activate'),
    onSuccess: invalidate, onError: error => message.error(errorText(error)),
  })
  const exportSuite = (suite: AgentEvalSuite) => downloadEvalJson(suite.name, {
    format: 'myclaw.eval-suite.v1', definition: { name: suite.name, description: suite.description,
      gate_policy: suite.gate_policy, cases: suite.cases.map(({ case_key, name, input_text, assertions, weight, tags }) => ({ case_key, name, input_text, assertions, weight, tags })) },
  })
  const runOptions = (runs.data || []).filter(terminal).map(item => ({ value: item.id,
    label: `${item.suite_name} v${item.suite_version_no} · ${new Date(item.created_at).toLocaleString()} · ${item.candidate_content_hash.slice(0, 8)} · ${pct(item.score)}` }))
  const selectedBaseline = runs.data?.find(item => item.id === baseline)
  const tags = [...new Set(suites.data?.flatMap(s => s.cases.flatMap(c => c.tags || [])) || [])]
  if (detail.isError) return <Alert type="error" showIcon message="无法访问该智能体的评测配置" />
  return <Space direction="vertical" size="large" className="w-full">
    <Space wrap><Typography.Text>当前保存草稿：{detail.data?.draft.draft_revision ?? '加载中'}</Typography.Text>
      <Button onClick={() => navigate(`/agents/${agentId}/config`)}>配置与发布</Button>
      <Button onClick={() => { detail.refetch(); suites.refetch(); runs.refetch() }}>刷新</Button>
    </Space>
    <Alert type="info" showIcon message="评测使用当前已保存草稿的固定快照，调用真实模型和已绑定工具，并产生实际用量。每个用例是独立 Chat；需要补充信息或审批时，可进入对应对话处理。" />
    {(suites.isError || runs.isError) && <Alert type="error" message="评测数据加载失败，请刷新重试" />}
    <Tabs activeKey={activeTab} onChange={setActiveTab} items={[
      { key: 'suites', label: '评测集', children: <Card extra={<Space wrap>
        <Select allowClear placeholder="按场景筛选" value={tagFilter} onChange={setTagFilter} style={{ width: 160 }} options={tags.map(tag => ({ value: tag, label: tag }))} />
        <Button onClick={() => openEditor()}>新建评测集</Button>
        <label className="cursor-pointer text-primary">导入 JSON<input className="hidden" type="file" accept=".json,application/json" onChange={async event => {
          const file = event.target.files?.[0]; event.target.value = ''
          if (!file) return
          if (file.size > 2_000_000) return void message.error('文件不能超过 2 MB')
          try { openEditor(undefined, parseEvalDefinition(await file.text())) } catch (error) { message.error(errorText(error)) }
        }} /></label>
      </Space>}>
        <Table rowKey="id" loading={suites.isLoading} dataSource={(suites.data || []).filter(s => !tagFilter || s.cases.some(c => c.tags?.includes(tagFilter)))} pagination={{ pageSize: 10 }} columns={[
          { title: '名称', dataIndex: 'name' }, { title: '版本', dataIndex: 'current_version_no', render: (v: number) => `v${v}` },
          { title: '用途', render: (_, s) => <Tag>{s.gate_policy.required_for_publish === false ? '日常回归' : '发布门禁'}</Tag> },
          { title: '状态', render: (_, s) => s.status === 'active' ? '已启用' : '已停用' },
          { title: '通过线', render: (_, s) => pct(s.gate_policy.min_pass_rate || '1') },
          { title: '用例', render: (_, s) => s.cases.length },
          { title: '操作', render: (_, s) => <Space wrap>
            <Button size="small" type="primary" disabled={!detail.data || s.status !== 'active'} loading={run.isPending} onClick={() => run.mutate(s)}>运行</Button>
            <Button size="small" onClick={() => openEditor(s)}>编辑</Button>
            <Button size="small" onClick={() => exportSuite(s)}>导出</Button>
            <Button size="small" disabled={state.isPending} onClick={() => state.mutate(s)}>{s.status === 'active' ? '停用' : '启用'}</Button>
          </Space> },
        ]} expandable={{ expandedRowRender: suite => <>
          <Typography.Paragraph>{suite.description || '暂无说明'}</Typography.Paragraph>
          <Table size="small" rowKey="id" pagination={false} dataSource={suite.cases} columns={[
            { title: '用例', dataIndex: 'name' }, { title: '标签', render: (_, item) => item.tags?.map(tag => <Tag key={tag}>{tag}</Tag>) },
            { title: '输入', dataIndex: 'input_text', ellipsis: true }, { title: '权重', dataIndex: 'weight' },
            { title: '断言', render: (_, item) => <Typography.Text code>{JSON.stringify(item.assertions)}</Typography.Text> },
          ]} />
        </> }} />
      </Card> },
      { key: 'runs', label: '回归运行', children: <Card title="最近 50 次评测">
        <Table rowKey="id" loading={runs.isLoading} dataSource={runs.data || []} pagination={{ pageSize: 10 }} columns={[
          { title: '评测集', render: (_, item) => `${item.suite_name} v${item.suite_version_no}` },
          { title: '配置指纹', render: (_, item) => <Typography.Text copyable={{ text: item.candidate_content_hash }}>{item.candidate_content_hash.slice(0, 12)}</Typography.Text> },
          { title: '状态', dataIndex: 'status', render: status },
          { title: '进度', render: (_, item) => <Progress size="small" percent={Math.round((item.passed_count + item.failed_count + item.error_count) / item.case_count * 100)} /> },
          { title: '加权通过率', dataIndex: 'score', render: pct },
          { title: '时间', dataIndex: 'created_at', render: (v: string) => new Date(v).toLocaleString() },
          { title: '操作', render: (_, item) => <Button size="small" onClick={() => downloadEvalJson(`eval-${item.id}`, item)}>导出报告</Button> },
        ]} expandable={{ expandedRowRender: item => <Table rowKey="id" size="small" dataSource={item.case_runs} pagination={false} columns={[
          { title: '用例', dataIndex: 'name' }, { title: '结果', dataIndex: 'status', render: status },
          { title: '运行状态', dataIndex: 'runtime_status', render: (v: string) => displayLabel(v) },
          { title: '错误', dataIndex: 'failure_code' },
          { title: '操作', render: (_, c) => <Button size="small" onClick={() => navigate(`/agents/${agentId}/chat/${c.session_id}`)}>查看过程</Button> },
        ]} expandable={{ expandedRowRender: c => <Space direction="vertical" className="w-full">
          <Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>输入：{c.input_text}</Typography.Paragraph>
          <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', maxHeight: 360, overflow: 'auto' }}>回答：{c.response_text ?? '暂无完整回答'}</Typography.Paragraph>
          <Checks items={c.assertion_results} />
        </Space> }} /> }} />
      </Card> },
      { key: 'compare', label: '版本对比', children: <Space direction="vertical" size="large" className="w-full">
        <Alert showIcon message="选择同一评测集版本的两次已结束运行，比较配置修改前后的结果；同一配置的重复运行可用于观察稳定性。" />
        <Space wrap><Select aria-label="基线运行" placeholder="基线运行" style={{ width: 420 }} options={runOptions} value={baseline} onChange={value => { setBaseline(value); setCandidate(undefined) }} />
          <Select aria-label="候选运行" placeholder="候选运行" style={{ width: 420 }} value={candidate} onChange={setCandidate} disabled={!baseline}
            options={runOptions.filter(o => o.value !== baseline && runs.data?.find(r => r.id === o.value)?.suite_version_id === selectedBaseline?.suite_version_id)} /></Space>
        {comparison.isError && <Alert type="error" message={errorText(comparison.error)} />}
        {comparison.isFetching && <Card loading />}
        {comparison.data && <>
          {comparison.data.same_candidate && <Alert type="info" message="两次运行的配置指纹一致，差异反映运行波动。" />}
          <Space size="large" wrap><Statistic title="通过率变化（百分点）" value={Number(comparison.data.score_delta) * 100} precision={1} />
            <Statistic title="退步用例" value={comparison.data.regressed_count} /><Statistic title="改善用例" value={comparison.data.improved_count} />
            <Button onClick={() => downloadEvalJson('eval-comparison', comparison.data)}>导出对比</Button></Space>
          <Table rowKey="case_key" dataSource={comparison.data.cases} columns={[
            { title: '用例', dataIndex: 'name' }, { title: '基线', dataIndex: 'baseline_status', render: status },
            { title: '候选', dataIndex: 'candidate_status', render: status },
            { title: '变化', dataIndex: 'change', render: (v: string) => <Tag color={v === 'regressed' ? 'error' : v === 'improved' ? 'success' : 'default'}>{({ regressed: '退步', improved: '改善', unchanged: '持平' } as Record<string, string>)[v]}</Tag> },
          ]} expandable={{ expandedRowRender: item => <>
            <div className="grid gap-4 md:grid-cols-2"><Card size="small" title="基线回答"><Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{item.baseline_response || '无完整回答'}</Typography.Paragraph></Card>
              <Card size="small" title="候选回答"><Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{item.candidate_response || '无完整回答'}</Typography.Paragraph></Card></div>
            <Checks items={item.candidate_assertions} />
          </> }} />
          <Table rowKey="tag" size="small" pagination={false} dataSource={comparison.data.tags} columns={[
            { title: '场景标签', dataIndex: 'tag' }, { title: '用例数', dataIndex: 'count' },
            { title: '基线通过数', dataIndex: 'baseline_passed' }, { title: '候选通过数', dataIndex: 'candidate_passed' },
          ]} />
        </>}
      </Space> },
    ]} />
    <Modal title={editing ? `编辑评测集 · 保存为 v${editing.current_version_no + 1}` : '新建评测集'} open={editorOpen} width={960}
      onCancel={() => setEditorOpen(false)} onOk={() => form.submit()} confirmLoading={save.isPending} okText="保存版本" destroyOnClose>
      <Form form={form} layout="vertical" onFinish={values => save.mutate(values)}><SuiteEditor agentId={agentId} form={form} /></Form>
    </Modal>
  </Space>
}

export default function EvalsPage() {
  const user = useAuthStore(state => state.user)
  const [params, setParams] = useSearchParams()
  const roles = user?.roles?.length ? user.roles : [user?.role || 'user']
  const allowed = roles.some(role => ['builder', 'admin'].includes(role))
  const agents = useQuery({ queryKey: ['eval-agents', user?.id], queryFn: () => evalsApi.agents(roles.includes('admin')), enabled: allowed })
  const agentId = params.get('agent') || ''
  if (!allowed) return <Alert type="warning" message="评测工作台仅供构建者和管理员使用" />
  return <div className="mx-auto max-w-7xl space-y-5">
    <div><Typography.Title level={3}>评测</Typography.Title><Typography.Paragraph type="secondary">沉淀真实问题，验证智能体配置，比较每次改动的效果。</Typography.Paragraph></div>
    <Select showSearch optionFilterProp="label" placeholder="选择要评测的智能体" style={{ width: 360 }} loading={agents.isLoading}
      value={agentId || undefined} onChange={value => setParams({ agent: value })} options={(agents.data || []).map(a => ({ value: a.id, label: a.name }))} />
    {agents.isError && <Alert type="error" message="智能体列表加载失败" />}
    {agentId ? <Workbench key={agentId} agentId={agentId} /> : <Empty description="选择智能体后管理评测集和回归结果" />}
  </div>
}
