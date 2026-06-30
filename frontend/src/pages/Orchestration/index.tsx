import { useParams, useNavigate } from 'react-router-dom'
import { useState, useCallback } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { Button, Space, Typography, Input, message, Card, Tag, Spin } from 'antd'
import { ArrowLeftOutlined, ThunderboltOutlined, CheckOutlined } from '@ant-design/icons'
import ReactFlow, {
  Node, Edge, Background, Controls, MiniMap,
  addEdge, useNodesState, useEdgesState, Connection,
} from 'reactflow'
import 'reactflow/dist/style.css'
import client from '../../api/client'

const STATUS_COLORS: Record<string, string> = {
  pending: '#d9d9d9', running: '#1677ff', completed: '#52c41a', failed: '#ff4d4f',
}

function agentNodesToFlow(graphJson: any): { nodes: Node[], edges: Edge[] } {
  const nodes: Node[] = (graphJson?.nodes || []).map((n: any, i: number) => ({
    id: n.id,
    data: { label: <div><strong>{n.agent_name}</strong><br /><small>{n.step_goal}</small></div> },
    position: { x: 150 + (i % 3) * 220, y: Math.floor(i / 3) * 150 + 50 },
    style: { background: STATUS_COLORS[n.status] || '#f5f5f5', border: '1px solid #d9d9d9', borderRadius: 8, padding: 12 },
  }))
  const edges: Edge[] = []
  for (const edge of (graphJson?.edges || [])) {
    const frm = Array.isArray(edge.from) ? edge.from : [edge.from]
    const to = Array.isArray(edge.to) ? edge.to : [edge.to]
    for (const f of frm) {
      for (const t of to) {
        if (f === 'start') continue
        edges.push({ id: `${f}-${t}`, source: f, target: t, animated: true })
      }
    }
  }
  return { nodes, edges }
}

export default function OrchestrationPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const [goal, setGoal] = useState('')
  const [graphJson, setGraphJson] = useState<any>(null)
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])

  const onConnect = useCallback((params: Connection) => setEdges(eds => addEdge(params, eds)), [])

  const generateMutation = useMutation({
    mutationFn: () => client.post(`/sessions/${sessionId}/group/orchestrate`, { goal }).then(r => r.data),
    onSuccess: (data) => {
      setGraphJson(data)
      const { nodes: n, edges: e } = agentNodesToFlow(data)
      setNodes(n)
      setEdges(e)
    },
    onError: () => message.error('图编排生成失败'),
  })

  const executeMutation = useMutation({
    mutationFn: () => client.post(`/sessions/${sessionId}/group/execute`).then(r => r.data),
    onSuccess: (data) => { message.success(`已启动 ${data.task_ids?.length} 个任务`); navigate('/tasks') },
    onError: () => message.error('执行失败'),
  })

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(-1)}>返回</Button>
        <Typography.Title level={5} style={{ margin: 0 }}>Group 编排图</Typography.Title>
      </div>

      <Card size="small">
        <Space.Compact style={{ width: '100%' }}>
          <Input
            value={goal}
            onChange={e => setGoal(e.target.value)}
            placeholder="描述你的目标，例如：为用户制定个性化随访计划"
            size="large"
          />
          <Button
            type="primary"
            onClick={() => generateMutation.mutate()}
            loading={generateMutation.isPending}
            size="large"
          >
            AI 编排
          </Button>
        </Space.Compact>
      </Card>

      <div style={{ flex: 1, border: '1px solid #f0f0f0', borderRadius: 8, overflow: 'hidden', position: 'relative' }}>
        {generateMutation.isPending && (
          <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(255,255,255,0.8)', zIndex: 10 }}>
            <Spin tip="AI 正在分析并编排..." size="large" />
          </div>
        )}
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          fitView
        >
          <Background />
          <Controls />
          <MiniMap />
        </ReactFlow>
      </div>

      {graphJson && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Space>
            <Typography.Text type="secondary">
              {graphJson.nodes?.length} 个 Agent，{graphJson.edges?.length} 条边
            </Typography.Text>
            <Button
              type="primary"
              icon={<CheckOutlined />}
              onClick={() => executeMutation.mutate()}
              loading={executeMutation.isPending}
            >
              确认执行
            </Button>
          </Space>
        </div>
      )}
    </div>
  )
}
