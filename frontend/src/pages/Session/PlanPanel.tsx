import { useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, Descriptions, Input, Modal, Space, Table, Tag, Typography } from 'antd'
import ReactFlow, { Background, Controls, type Edge, type Node } from 'reactflow'
import 'reactflow/dist/style.css'
import type { PlanDependency, PlanExecution, PlanNode, PlanWorkItem } from '@/api/sessions'

interface Props {
  plan: PlanExecution | undefined
  workItems: PlanWorkItem[]
  busy: boolean
  onSave: (value: { goal: string; deliverable: string; nodes: PlanNode[]; dependencies: PlanDependency[] }) => Promise<void>
  onRevise: (instruction: string) => Promise<void>
  onConfirm: () => Promise<void>
}

const colors: Record<string, string> = {
  queued: 'default', running: 'processing', waiting_agent: 'warning', waiting_user: 'warning',
  succeeded: 'success', failed: 'error', blocked: 'error', cancelled: 'default', abandoned: 'default',
}

export default function PlanPanel({ plan, workItems, busy, onSave, onRevise, onConfirm }: Props) {
  const [editing, setEditing] = useState(false)
  const [goal, setGoal] = useState('')
  const [deliverable, setDeliverable] = useState('')
  const [nodesJson, setNodesJson] = useState('[]')
  const [dependenciesJson, setDependenciesJson] = useState('[]')
  const [revisionInstruction, setRevisionInstruction] = useState('')
  const workByNode = useMemo(() => new Map(workItems.filter(item => item.plan_node_id).map(item => [item.plan_node_id!, item])), [workItems])
  const graphNodes = useMemo<Node[]>(() => (plan?.nodes ?? []).map((node, index) => {
    const status = workByNode.get(node.id)?.status || (plan?.definition_status === 'frozen' ? 'not_started' : 'proposal')
    return {
      id: node.id,
      position: { x: (index % 3) * 260, y: Math.floor(index / 3) * 140 },
      data: { label: <div className="min-w-40"><b>{node.title}</b><div className="mt-1 text-xs">{node.node_key} · {status}</div></div> },
      style: { borderColor: status === 'failed' || status === 'blocked' ? '#ef4444' : status === 'succeeded' ? '#22c55e' : '#94a3b8', borderRadius: 10 },
    }
  }), [plan?.nodes, plan?.definition_status, workByNode])
  const graphEdges = useMemo<Edge[]>(() => {
    const byKey = new Map((plan?.nodes ?? []).map(node => [node.node_key, node.id]))
    return (plan?.dependencies ?? []).map((edge, index) => ({
      id: `${edge.predecessor_node_key}-${edge.successor_node_key}-${index}`,
      source: byKey.get(edge.predecessor_node_key)!, target: byKey.get(edge.successor_node_key)!,
      label: edge.dependency_type, animated: plan?.run.lifecycle === 'running',
    })).filter(edge => edge.source && edge.target)
  }, [plan?.nodes, plan?.dependencies, plan?.run.lifecycle])

  useEffect(() => {
    if (!plan) return
    setGoal(plan.goal)
    setDeliverable(plan.deliverable)
    setNodesJson(JSON.stringify(plan.nodes.map(({ node_key, title, goal, acceptance_criteria, assigned_agent_id }) =>
      ({ node_key, title, goal, acceptance_criteria, assigned_agent_id })), null, 2))
    setDependenciesJson(JSON.stringify(plan.dependencies, null, 2))
  }, [plan?.id, plan?.version])

  if (!plan) {
    return <div className="py-20 text-center"><div className="mb-3 text-3xl">◇</div><p className="text-lg font-semibold">创建一个可确认的 Plan</p><p className="mt-2 text-sm text-muted-foreground">Owner 会先生成关系化工作图；你确认前不会开始正式执行。</p></div>
  }

  const generating = plan.definition_status === 'draft' && plan.run.lifecycle === 'running'
  const awaiting = plan.definition_status === 'awaiting_confirmation'
  const save = async () => {
    try {
      const nodes = JSON.parse(nodesJson) as PlanNode[]
      const dependencies = JSON.parse(dependenciesJson) as PlanDependency[]
      await onSave({ goal, deliverable, nodes, dependencies })
      setEditing(false)
    } catch (error: any) {
      Modal.error({ title: '关系图格式无效', content: error?.response?.data?.error?.details?.issues?.join('\n') || error.message })
    }
  }

  return <div className="space-y-4">
    <Card title={<Space><span>Plan</span><Tag>{plan.definition_status}</Tag><Tag>revision {plan.graph_revision}</Tag></Space>}
      extra={awaiting && <Space><Button onClick={() => setEditing(true)}>编辑关系图</Button><Button type="primary" loading={busy} onClick={onConfirm}>确认并冻结</Button></Space>}>
      <Descriptions column={1} size="small">
        <Descriptions.Item label="目标">{plan.goal}</Descriptions.Item>
        <Descriptions.Item label="交付物">{plan.deliverable || '生成中…'}</Descriptions.Item>
        <Descriptions.Item label="执行状态"><Tag color={plan.run.lifecycle === 'completed' ? 'success' : 'processing'}>{plan.run.lifecycle}</Tag>{plan.run.outcome && <Tag>{plan.run.outcome}</Tag>}</Descriptions.Item>
        {plan.frozen_hash && <Descriptions.Item label="冻结哈希"><Typography.Text code copyable>{plan.frozen_hash}</Typography.Text></Descriptions.Item>}
      </Descriptions>
      {generating && <Alert className="mt-4" type="info" showIcon message="Owner 正在生成结构化 Plan Proposal；页面可以安全关闭。" />}
      {awaiting && <Alert className="mt-4" type="warning" showIcon message="图尚未执行。确认后节点、依赖、参与 Agent 与 AgentVersion 都会冻结。" />}
      {plan.definition_status === 'frozen' && <Alert className="mt-4" type="success" showIcon message="关系图已冻结；后续结果只改变 Work Item 与 Run 状态。" />}
    </Card>

    {plan.nodes.length > 0 && <Card title="Work Item Graph">
      <div className="mb-4 h-80 overflow-hidden rounded-lg border bg-muted/20">
        <ReactFlow nodes={graphNodes} edges={graphEdges} fitView nodesDraggable={false}
          nodesConnectable={false} elementsSelectable={false} proOptions={{ hideAttribution: true }}>
          <Background /><Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <Table rowKey="id" pagination={false} size="small" dataSource={plan.nodes} columns={[
        { title: '节点', render: (_, node) => <div><b>{node.title}</b><div className="text-xs text-muted-foreground">{node.node_key}</div></div> },
        { title: '目标 / 验收', render: (_, node) => <div className="max-w-md"><div>{node.goal}</div><div className="mt-1 text-xs text-muted-foreground">验收：{node.acceptance_criteria}</div></div> },
        { title: 'Agent', dataIndex: 'assigned_agent_id', render: value => <Typography.Text code>{String(value).slice(0, 12)}</Typography.Text> },
        { title: '状态', render: (_, node) => { const status = workByNode.get(node.id)?.status || (plan.definition_status === 'frozen' ? 'not_started' : 'proposal'); return <Tag color={colors[status]}>{status}</Tag> } },
      ]} />
      {plan.dependencies.length > 0 && <div className="mt-4 space-y-1 border-t pt-3 text-sm text-muted-foreground">
        {plan.dependencies.map((edge, index) => <div key={`${edge.predecessor_node_key}-${edge.successor_node_key}-${index}`}><Tag>{edge.dependency_type}</Tag>{edge.predecessor_node_key} → {edge.successor_node_key}</div>)}
      </div>}
    </Card>}

    {awaiting && <Card title="让 Owner 重新生成">
      <Space.Compact className="w-full"><Input value={revisionInstruction} onChange={event => setRevisionInstruction(event.target.value)} placeholder="例如：把调研拆成两个并行节点，并增加风险验收条件" /><Button loading={busy} disabled={!revisionInstruction.trim()} onClick={async () => { await onRevise(revisionInstruction.trim()); setRevisionInstruction('') }}>重新生成</Button></Space.Compact>
    </Card>}

    <Modal title="编辑关系化 Plan 图" open={editing} onCancel={() => setEditing(false)} onOk={save} confirmLoading={busy} width={880} okText="保存新 revision">
      <Alert className="mb-4" type="info" message="保存会执行 node key、DAG、直接绑定与用户访问校验，并使用 If-Match 防止覆盖并发编辑。" />
      <label className="mb-1 block text-sm font-medium">目标</label><Input.TextArea className="mb-3" value={goal} onChange={event => setGoal(event.target.value)} autoSize />
      <label className="mb-1 block text-sm font-medium">交付物</label><Input.TextArea className="mb-3" value={deliverable} onChange={event => setDeliverable(event.target.value)} autoSize />
      <label className="mb-1 block text-sm font-medium">节点 JSON</label><Input.TextArea className="mb-3 font-mono" value={nodesJson} onChange={event => setNodesJson(event.target.value)} autoSize={{ minRows: 10, maxRows: 18 }} />
      <label className="mb-1 block text-sm font-medium">依赖 JSON</label><Input.TextArea className="font-mono" value={dependenciesJson} onChange={event => setDependenciesJson(event.target.value)} autoSize={{ minRows: 5, maxRows: 12 }} />
    </Modal>
  </div>
}
