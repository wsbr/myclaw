import { useEffect, useState } from 'react'
import { Alert, Button, Card, Descriptions, Empty, Input, Modal, Space, Table, Tag, Typography } from 'antd'
import ReactFlow, { Background, Controls, type Edge, type Node } from 'reactflow'
import 'reactflow/dist/style.css'
import type { GroupCollaborationEdge, GroupFormationProposal, GroupFormationRevision, GroupMessage, GroupNode, GroupRun, GroupSuggestedEdge } from '@/api/sessions'

interface Props {
  proposal?: GroupFormationProposal
  nodes: GroupNode[]
  runs: GroupRun[]
  messages: GroupMessage[]
  suggestedEdges: GroupSuggestedEdge[]
  collaborationEdges: GroupCollaborationEdge[]
  busy: boolean
  onSave: (revision: GroupFormationRevision) => Promise<void>
  onConfirm: () => Promise<void>
  onReject: () => Promise<void>
}

export default function GroupPanel({ proposal, nodes, runs, messages, suggestedEdges, collaborationEdges, busy, onSave, onConfirm, onReject }: Props) {
  const [editing, setEditing] = useState(false)
  const [goal, setGoal] = useState('')
  const [bootstrap, setBootstrap] = useState('')
  const [participants, setParticipants] = useState('[]')
  const [workItems, setWorkItems] = useState('[]')
  const [edges, setEdges] = useState('[]')
  const graphNodes = nodes.map<Node>((node, index) => ({
    id: node.id,
    position: { x: (index % 3) * 260, y: Math.floor(index / 3) * 150 },
    data: { label: <div className="min-w-40"><b>{node.agent_id.slice(0, 12)}</b><div className="mt-1 text-xs">{node.is_entry ? 'entry · ' : ''}{node.availability}</div></div> },
    style: { borderColor: node.availability === 'available' ? '#22c55e' : '#ef4444', borderRadius: 10 },
  }))
  const graphEdges: Edge[] = [
    ...suggestedEdges.map((edge, index) => ({
      id: `suggested-${index}-${edge.source_node_id}-${edge.target_node_id}`,
      source: edge.source_node_id, target: edge.target_node_id, label: edge.label,
      style: { strokeDasharray: '6 4', stroke: '#94a3b8' },
    })),
    ...collaborationEdges.map(edge => ({
      id: edge.id, source: edge.source_node_id, target: edge.target_node_id,
      label: 'invoke_agent', animated: true, style: { stroke: '#2563eb', strokeWidth: 2 },
    })),
  ]

  useEffect(() => {
    const revision = proposal?.current_revision
    if (!revision) return
    setGoal(revision.goal); setBootstrap(revision.bootstrap_context)
    setParticipants(JSON.stringify(revision.participants.map(({ participant_key, agent_id, is_entry, initial_responsibility }) =>
      ({ participant_key, agent_id, is_entry, initial_responsibility })), null, 2))
    setWorkItems(JSON.stringify(revision.initial_work_items.map(({ work_item_key, participant_key, title, goal, acceptance_criteria }) =>
      ({ work_item_key, participant_key, title, goal, acceptance_criteria })), null, 2))
    setEdges(JSON.stringify(revision.suggested_edges.map(({ source_participant_key, target_participant_key, label, rationale }) =>
      ({ source_participant_key, target_participant_key, label, rationale })), null, 2))
  }, [proposal?.id, proposal?.version])

  if (!proposal) return <div className="py-20 text-center"><div className="mb-3 text-3xl">◎</div><p className="text-lg font-semibold">组建一个固定 Agent Group</p><p className="mt-2 text-sm text-muted-foreground">先描述目标；入口 Agent 会生成参与者、初始 Work Item 与建议协作边，确认前不会开始执行。</p></div>

  const revision = proposal.current_revision
  const awaiting = proposal.status === 'awaiting_confirmation' && !!revision
  const save = async () => {
    try {
      await onSave({ ...revision!, goal, bootstrap_context: bootstrap,
        participants: JSON.parse(participants), initial_work_items: JSON.parse(workItems),
        suggested_edges: JSON.parse(edges) })
      setEditing(false)
    } catch (error: any) {
      Modal.error({ title: 'Group Formation 格式无效', content: error?.response?.data?.error?.details?.issues?.join('\n') || error.message })
    }
  }

  return <div className="space-y-4">
    <Card title={<Space><span>Group Formation</span><Tag>{proposal.status}</Tag>{revision && <Tag>revision {revision.revision_no}</Tag>}</Space>}
      extra={awaiting && <Space><Button onClick={() => setEditing(true)}>编辑组图</Button><Button danger onClick={onReject}>拒绝</Button><Button type="primary" loading={busy} onClick={onConfirm}>确认并冻结</Button></Space>}>
      {proposal.status === 'generating' && <Alert type="info" showIcon message="入口 Agent 正在生成关系化 Group Formation；页面可以安全关闭。" />}
      {awaiting && <Alert type="warning" showIcon message="确认后参与节点与初始定义永久冻结；每个 GroupRun 会单独固定所有节点的 AgentVersion/不可用状态。" />}
      {proposal.status === 'confirmed' && <Alert type="success" showIcon message="Group 已开放；后续追问会创建新的 GroupRun，不会复活已完成的 Run。" />}
      {revision && <Descriptions className="mt-4" column={1} size="small">
        <Descriptions.Item label="目标">{revision.goal}</Descriptions.Item>
        <Descriptions.Item label="共享 Bootstrap">{revision.bootstrap_context}</Descriptions.Item>
        <Descriptions.Item label="内容哈希"><Typography.Text code copyable>{revision.content_hash}</Typography.Text></Descriptions.Item>
      </Descriptions>}
    </Card>

    {revision && <Card title="参与节点与初始职责"><Table rowKey="participant_key" pagination={false} size="small"
      dataSource={revision.participants} columns={[
        { title: '节点', render: (_, row) => <Space><b>{row.participant_key}</b>{row.is_entry && <Tag color="blue">entry</Tag>}</Space> },
        { title: 'Agent', dataIndex: 'agent_id', render: value => <Typography.Text code>{String(value).slice(0, 14)}</Typography.Text> },
        { title: '初始职责', dataIndex: 'initial_responsibility' },
      ]} /></Card>}

    {proposal.status === 'confirmed' && <>
      <Card title="Group Agent Graph" extra={<Space><Tag>虚线：建议边</Tag><Tag color="blue">实线：实际调用</Tag></Space>}>
        <div className="h-80 overflow-hidden rounded-lg border bg-muted/20">
          <ReactFlow nodes={graphNodes} edges={graphEdges} fitView nodesDraggable={false}
            nodesConnectable={false} elementsSelectable={false} proOptions={{ hideAttribution: true }}>
            <Background /><Controls showInteractive={false} />
          </ReactFlow>
        </div>
      </Card>
      <Card title="固定 Group 节点"><Table rowKey="id" pagination={false} size="small" dataSource={nodes} columns={[
        { title: 'Agent', render: (_, row) => <Space><Typography.Text code>{row.agent_id.slice(0, 14)}</Typography.Text>{row.is_entry && <Tag color="blue">entry</Tag>}</Space> },
        { title: '职责', dataIndex: 'initial_responsibility' },
        { title: '当前可用性', render: (_, row) => <Tag color={row.availability === 'available' ? 'success' : 'error'}>{row.availability}{row.unavailable_reason ? ` · ${row.unavailable_reason}` : ''}</Tag> },
      ]} /></Card>
      <Card title="GroupRun 历史">{runs.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚无 GroupRun" /> : runs.map(run =>
        <div key={run.id} className="mb-3 rounded-lg border p-3"><Space><Tag color={run.run.lifecycle === 'completed' ? 'success' : 'processing'}>{run.display_phase}</Tag>{run.run.outcome && <Tag>{run.run.outcome}</Tag>}</Space><div className="mt-2 text-sm">{run.goal}</div>{run.run.result_summary && <div className="mt-2 whitespace-pre-wrap text-sm text-muted-foreground">{run.run.result_summary}</div>}</div>)}</Card>
      {messages.length > 0 && <Card title="当前 GroupRun 共享记录"><div className="space-y-3">{messages.map(item =>
        <div key={item.id} className={`flex ${item.sender_type === 'user' ? 'justify-end' : 'justify-start'}`}><div className={`max-w-[82%] whitespace-pre-wrap rounded-2xl px-4 py-2.5 text-sm ${item.sender_type === 'user' ? 'bg-primary text-primary-foreground' : 'bg-muted'}`}>{item.content}</div></div>)}</div></Card>}
    </>}

    <Modal title="编辑 Group Formation revision" open={editing} onCancel={() => setEditing(false)} onOk={save} confirmLoading={busy} width={900} okText="保存新 revision">
      <Alert className="mb-4" type="info" message="保存会重新校验 Agent 访问、group_enabled、唯一 entry、Work Item 引用和建议边；原 revision 保持不可变。" />
      <label className="mb-1 block text-sm font-medium">目标</label><Input.TextArea className="mb-3" value={goal} onChange={event => setGoal(event.target.value)} autoSize />
      <label className="mb-1 block text-sm font-medium">共享 Bootstrap</label><Input.TextArea className="mb-3" value={bootstrap} onChange={event => setBootstrap(event.target.value)} autoSize={{ minRows: 3 }} />
      <label className="mb-1 block text-sm font-medium">Participants JSON</label><Input.TextArea className="mb-3 font-mono" value={participants} onChange={event => setParticipants(event.target.value)} autoSize={{ minRows: 8, maxRows: 16 }} />
      <label className="mb-1 block text-sm font-medium">Initial Work Items JSON</label><Input.TextArea className="mb-3 font-mono" value={workItems} onChange={event => setWorkItems(event.target.value)} autoSize={{ minRows: 8, maxRows: 16 }} />
      <label className="mb-1 block text-sm font-medium">Suggested Edges JSON</label><Input.TextArea className="font-mono" value={edges} onChange={event => setEdges(event.target.value)} autoSize={{ minRows: 5, maxRows: 12 }} />
    </Modal>
  </div>
}
