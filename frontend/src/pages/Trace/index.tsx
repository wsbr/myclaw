import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { WS_BASE } from '../../api/client'
import { Layout, Tree, Card, Typography, Tag, Space, Button, Descriptions, Spin, Empty, Collapse, Badge } from 'antd'
import { ArrowLeftOutlined, BranchesOutlined } from '@ant-design/icons'
import { sessionsApi } from '../../api/sessions'
import { useState, useEffect, useRef } from 'react'
import type { DataNode } from 'antd/es/tree'

const { Sider, Content } = Layout

const SPAN_TYPE_COLORS: Record<string, string> = {
  llm_call: 'blue', tool_call: 'orange', agent_call: 'purple',
  memory: 'cyan', event: 'green', plan_step: 'volcano',
}

function buildTree(spans: any[]): DataNode[] {
  const map = new Map<string, DataNode & { raw: any }>()
  const roots: (DataNode & { raw: any })[] = []
  for (const s of spans) {
    const node = { key: s.id, title: <Space><Tag color={SPAN_TYPE_COLORS[s.span_type]}>{s.span_type}</Tag>{s.name}</Space>, raw: s, children: [] as DataNode[] }
    map.set(s.id, node)
  }
  for (const s of spans) {
    const node = map.get(s.id)!
    if (s.parent_span_id && map.has(s.parent_span_id)) {
      map.get(s.parent_span_id)!.children!.push(node)
    } else {
      roots.push(node)
    }
  }
  return roots
}

export default function TracePage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [selectedRunner, setSelectedRunner] = useState<string | null>(null)
  const [selectedSpan, setSelectedSpan] = useState<any | null>(null)
  const [liveEvent, setLiveEvent] = useState<string | null>(null)

  // WebSocket — 接收实时 span 事件并刷新 spans
  useEffect(() => {
    if (!sessionId) return
    const ws = new WebSocket(`${WS_BASE}/ws/sessions/${sessionId}`)
    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data)
        if (['span_started', 'span_completed', 'runner_completed'].includes(msg.type)) {
          setLiveEvent(msg.type)
          queryClient.invalidateQueries({ queryKey: ['spans', selectedRunner] })
          queryClient.invalidateQueries({ queryKey: ['runners', sessionId] })
        }
      } catch {}
    }
    return () => ws.close()
  }, [sessionId, selectedRunner, queryClient])

  const { data: runners } = useQuery({
    queryKey: ['runners', sessionId],
    queryFn: () => sessionsApi.listRunners(sessionId!),
    enabled: !!sessionId,
  })

  const { data: spans, isLoading: spansLoading } = useQuery({
    queryKey: ['spans', selectedRunner],
    queryFn: () => sessionsApi.listSpans(sessionId!, selectedRunner!),
    enabled: !!selectedRunner,
  })

  const treeData = spans ? buildTree(spans) : []

  const decompressSnapshot = (raw?: unknown) => {
    if (!raw) return null
    if (typeof raw === 'object') return raw
    if (typeof raw !== 'string') return null
    try { return JSON.parse(atob(raw)) } catch { return null }
  }

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div style={{ padding: '12px 0', display: 'flex', gap: 8, alignItems: 'center', justifyContent: 'space-between' }}>
        <Space>
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(-1)}>返回</Button>
          <Typography.Title level={5} style={{ margin: 0 }}>
            <BranchesOutlined /> Trace 查看器 — Session: {sessionId?.slice(0, 12)}
          </Typography.Title>
        </Space>
        {liveEvent && (
          <Badge status="processing" text={<Typography.Text type="secondary" style={{ fontSize: 12 }}>实时更新中</Typography.Text>} />
        )}
      </div>

      <div style={{ marginBottom: 12 }}>
        <Space wrap>
          {runners?.map((r: any) => (
            <Button
              key={r.id}
              type={r.id === selectedRunner ? 'primary' : 'default'}
              size="small"
              onClick={() => { setSelectedRunner(r.id); setSelectedSpan(null) }}
            >
              Runner #{r.id.slice(0, 8)} <Tag color={r.status === 'completed' ? 'success' : 'error'}>{r.status}</Tag>
            </Button>
          ))}
        </Space>
      </div>

      <Layout style={{ flex: 1, border: '1px solid #f0f0f0', borderRadius: 8 }}>
        <Sider theme="light" width={320} style={{ padding: 12, borderRight: '1px solid #f0f0f0', overflowY: 'auto' }}>
          {!selectedRunner && <Empty description="选择一个 Runner 查看 Span 树" />}
          {spansLoading && <Spin />}
          {treeData.length > 0 && (
            <Tree
              treeData={treeData}
              onSelect={(_, { node }) => setSelectedSpan((node as any).raw)}
              showLine
            />
          )}
        </Sider>

        <Content style={{ padding: 16, overflowY: 'auto' }}>
          {!selectedSpan && <Empty description="点击左侧 Span 查看详情" />}
          {selectedSpan && (
            <Space direction="vertical" style={{ width: '100%' }} size={16}>
              <Descriptions bordered size="small" column={2}>
                <Descriptions.Item label="Span ID">{selectedSpan.id.slice(0, 16)}</Descriptions.Item>
                <Descriptions.Item label="类型"><Tag color={SPAN_TYPE_COLORS[selectedSpan.span_type]}>{selectedSpan.span_type}</Tag></Descriptions.Item>
                <Descriptions.Item label="名称">{selectedSpan.name}</Descriptions.Item>
                <Descriptions.Item label="状态"><Tag color={selectedSpan.status === 'completed' ? 'success' : 'error'}>{selectedSpan.status}</Tag></Descriptions.Item>
                <Descriptions.Item label="耗时">{selectedSpan.duration_ms}ms</Descriptions.Item>
                <Descriptions.Item label="Tokens">{(selectedSpan.input_tokens || 0) + (selectedSpan.output_tokens || 0)}</Descriptions.Item>
              </Descriptions>

              {selectedSpan.error && (
                <Card size="small" title="错误" style={{ borderColor: '#ff4d4f' }}>
                  <Typography.Text type="danger">{selectedSpan.error}</Typography.Text>
                </Card>
              )}

              <Collapse>
                <Collapse.Panel header="快照（执行时上下文）" key="snapshot">
                  <pre style={{ fontSize: 12, overflow: 'auto', maxHeight: 400 }}>
                    {JSON.stringify(decompressSnapshot(selectedSpan.snapshot), null, 2)}
                  </pre>
                </Collapse.Panel>
                {selectedSpan.request && (
                  <Collapse.Panel header="请求" key="request">
                    <pre style={{ fontSize: 12, overflow: 'auto', maxHeight: 300 }}>
                      {JSON.stringify(decompressSnapshot(selectedSpan.request), null, 2)}
                    </pre>
                  </Collapse.Panel>
                )}
                {selectedSpan.response && (
                  <Collapse.Panel header="响应" key="response">
                    <pre style={{ fontSize: 12, overflow: 'auto', maxHeight: 300 }}>
                      {JSON.stringify(decompressSnapshot(selectedSpan.response), null, 2)}
                    </pre>
                  </Collapse.Panel>
                )}
              </Collapse>

              <Button
                icon={<BranchesOutlined />}
                onClick={() => {
                  // Fork session from this span
                  const modifications = { note: 'User initiated fork' }
                  sessionsApi.fork(sessionId!, { runner_id: selectedRunner!, span_id: selectedSpan.id, modifications })
                    .then(s => navigate(`/sessions/${s.id}/trace`))
                }}
              >
                从此节点 Fork 重跑
              </Button>
            </Space>
          )}
        </Content>
      </Layout>
    </div>
  )
}
