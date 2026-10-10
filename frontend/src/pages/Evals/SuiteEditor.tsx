import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Collapse, Form, Input, InputNumber, Modal, Select, Space, Switch, Table, Typography, message } from 'antd'
import type { FormInstance } from 'antd'
import { evalsApi } from '@/api/evals'
import type { AgentEvalCaseDefinition } from '@/api/agents'

const listAssertions = [
  ['contains_all', '答案必须包含'], ['contains_none', '答案不得包含'],
  ['require_tools', '必须调用的工具'], ['require_successful_tools', '必须执行成功的工具'], ['forbid_tools', '禁止调用的工具'],
]

export default function SuiteEditor({ agentId, form }: { agentId: string; form: FormInstance }) {
  const [importOpen, setImportOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [preview, setPreview] = useState<AgentEvalCaseDefinition | null>(null)
  const [runId, setRunId] = useState('')
  const sources = useQuery({ queryKey: ['eval-sources', agentId], queryFn: () => evalsApi.sources(agentId), enabled: importOpen })
  const cases = Form.useWatch('cases', form) as AgentEvalCaseDefinition[] | undefined
  const load = async (id: string) => {
    setLoading(true); setPreview(null)
    try { setPreview(await evalsApi.preview(agentId, id)) }
    catch { message.error('无法导入：仅支持当前账号有权访问的已结束 Chat 运行') }
    finally { setLoading(false) }
  }
  return <>
    <Form.Item name="name" label="评测集名称" rules={[{ required: true, whitespace: true }]}><Input maxLength={200} /></Form.Item>
    <Form.Item name="description" label="说明"><Input.TextArea rows={2} /></Form.Item>
    <Space align="start" wrap>
      <Form.Item name="min_pass_rate" label="最低加权通过率" rules={[{ required: true }]}><InputNumber min={0} max={1} step={0.05} /></Form.Item>
      <Form.Item name="required_for_publish" label="作为发布门禁" valuePropName="checked"><Switch /></Form.Item>
    </Space>
    <Alert className="mb-4" type="info" showIcon message="每个用例独立执行；同一用例的所有断言通过才得分。开启发布门禁后，配置或评测集版本变化需要重新评测。" />
    <Form.List name="cases" rules={[{ validator: async (_, values) => {
      if (!values?.length || values.length > 20) throw new Error('每个评测集需要 1–20 个用例')
      const keys = values.map((item: AgentEvalCaseDefinition) => item.case_key?.trim())
      if (new Set(keys).size !== keys.length) throw new Error('用例标识不能重复')
    } }]}>
      {(fields, { add, remove }, { errors }) => <>
        <Space className="mb-3" wrap>
          <Button disabled={fields.length >= 20} onClick={() => add({ case_key: `case-${crypto.randomUUID().slice(0, 8)}`, name: '', input_text: '', weight: 1, assertions: { min_chars: 1 }, tags: [] })}>添加用例</Button>
          <Button disabled={fields.length >= 20} onClick={() => { setPreview(null); setImportOpen(true) }}>从运行导入</Button>
          <Typography.Text type="secondary">{fields.length}/20 个用例</Typography.Text>
        </Space>
        <Collapse items={fields.map(field => ({ key: String(field.key), forceRender: true,
          label: `${field.name + 1}. ${cases?.[field.name]?.name || '新用例'}`,
          extra: <Button danger size="small" onClick={event => { event.stopPropagation(); remove(field.name) }}>移除</Button>,
          children: <>
            <Form.Item name={[field.name, 'source_run_id']} hidden><Input /></Form.Item>
            <Space align="start" wrap>
              <Form.Item name={[field.name, 'case_key']} label="用例标识" rules={[{ required: true, whitespace: true }]}><Input maxLength={100} /></Form.Item>
              <Form.Item name={[field.name, 'name']} label="名称" rules={[{ required: true, whitespace: true }]}><Input maxLength={200} /></Form.Item>
              <Form.Item name={[field.name, 'weight']} label="权重" rules={[{ required: true }]}><InputNumber min={0.0001} precision={4} max={99999999} /></Form.Item>
            </Space>
            <Form.Item name={[field.name, 'tags']} label="场景标签"><Select mode="tags" tokenSeparators={[',']} maxCount={20} /></Form.Item>
            <Form.Item name={[field.name, 'input_text']} label="用户输入" rules={[{ required: true, whitespace: true }]}><Input.TextArea autoSize={{ minRows: 3, maxRows: 12 }} /></Form.Item>
            {listAssertions.map(([key, label]) => <Form.Item key={key} name={[field.name, 'assertions', key]} label={label}><Select mode="tags" placeholder="输入后按回车添加" /></Form.Item>)}
            <Space align="start" wrap>
              <Form.Item name={[field.name, 'assertions', 'min_chars']} label="最少字符"><InputNumber min={0} precision={0} /></Form.Item>
              <Form.Item name={[field.name, 'assertions', 'max_chars']} label="最多字符"><InputNumber min={0} precision={0} /></Form.Item>
            </Space>
            <Collapse items={[{ key: 'cost', label: '成本上限（可选）', children: <Space align="start" wrap>
              <Form.Item name={[field.name, 'assertions', 'max_cost', 'currency']} label="币种"><Input placeholder="USD" maxLength={3} /></Form.Item>
              <Form.Item name={[field.name, 'assertions', 'max_cost', 'amount']} label="金额"><InputNumber min={0} stringMode /></Form.Item>
              <Form.Item name={[field.name, 'assertions', 'max_cost', 'allow_unknown']} label="允许成本未知" valuePropName="checked"><Switch /></Form.Item>
            </Space> }]} />
            {cases?.[field.name]?.source_run_id && <Typography.Paragraph className="mt-3" type="secondary">来源运行：{cases[field.name].source_run_id}。历史回答仅作参考，不参与模型输入。</Typography.Paragraph>}
          </>,
        }))} />
        <Form.ErrorList errors={errors} />
      </>}
    </Form.List>
    <Modal title="从真实运行导入用例" open={importOpen} width={850} footer={null} onCancel={() => setImportOpen(false)}>
      <Alert showIcon className="mb-3" message="仅导入本轮输入，不携带历史对话、记忆或文件；依赖这些上下文的用例请补充为自包含输入。历史错误和工具调用不会自动成为正确标准。" />
      <Space.Compact className="mb-3 w-full"><Input value={runId} onChange={event => setRunId(event.target.value)} placeholder="粘贴完整运行 ID" /><Button loading={loading} disabled={!runId.trim()} onClick={() => load(runId.trim())}>预览</Button></Space.Compact>
      {sources.isError && <Alert type="error" message="运行列表加载失败" />}
      <Table size="small" rowKey="run_id" loading={sources.isLoading} dataSource={sources.data || []} pagination={{ pageSize: 5 }} columns={[
        { title: '输入', dataIndex: 'input_preview', ellipsis: true }, { title: '状态', dataIndex: 'status' },
        { title: '时间', dataIndex: 'created_at', render: (value: string) => new Date(value).toLocaleString() },
        { title: '操作', render: (_, row) => <Button size="small" disabled={loading} onClick={() => load(row.run_id)}>预览</Button> },
      ]} />
      {preview && <Card size="small" title="导入预览">
        <Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{preview.input_text}</Typography.Paragraph>
        <Typography.Paragraph type="secondary" style={{ whiteSpace: 'pre-wrap', maxHeight: 200, overflow: 'auto' }}>{preview.source_snapshot?.response_text || '没有完整回答'}</Typography.Paragraph>
        <Typography.Paragraph>观察到的工具：{preview.source_snapshot?.tools?.map(t => `${t.key} (${t.status})`).join('、') || '无'}</Typography.Paragraph>
        <Button type="primary" onClick={() => {
          const current = form.getFieldValue('cases') || []
          if (current.length >= 20) return void message.error('最多 20 个用例')
          if (current.some((item: AgentEvalCaseDefinition) => item.case_key === preview.case_key)) return void message.error('该运行已在用例列表中')
          form.setFieldValue('cases', [...current, preview]); setImportOpen(false)
          message.success('已加入编辑器，请完善断言后保存')
        }}>加入用例</Button>
      </Card>}
    </Modal>
  </>
}
