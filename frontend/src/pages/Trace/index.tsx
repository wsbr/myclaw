import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Button, Card, Collapse, Descriptions, Empty, Select, Spin, Tag, Timeline, Typography } from 'antd'
import { ArrowLeftOutlined } from '@ant-design/icons'
import { ExecutionEvent, sessionsApi } from '@/api/sessions'

export default function TracePage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const [eventType, setEventType] = useState<string | undefined>()
  const { data: session } = useQuery({
    queryKey: ['session', sessionId], queryFn: () => sessionsApi.get(sessionId!), enabled: !!sessionId,
  })
  const { data, isLoading } = useQuery({
    queryKey: ['timeline', sessionId],
    queryFn: async () => {
      let cursor = 0
      const items: ExecutionEvent[] = []
      while (true) {
        const page = await sessionsApi.getEvents(sessionId!, cursor)
        items.push(...page.items)
        if (!page.has_more || page.scanned_through_sequence <= cursor) break
        cursor = page.scanned_through_sequence
      }
      return items
    },
    enabled: !!sessionId,
  })
  const types = [...new Set((data ?? []).map(item => item.event_type))]
  const events = (data ?? []).filter(item => !eventType || item.event_type === eventType)
  const runIds = [...new Set((data ?? []).flatMap(item => item.run_id ? [item.run_id] : []))]
  const { data: runners } = useQuery({
    queryKey: ['run-runners', sessionId, runIds.join(',')],
    queryFn: async () => (await Promise.all(runIds.map(id => sessionsApi.getRunRunners(id)))).flat(),
    enabled: runIds.length > 0,
  })
  const runnerIds = [...new Set((runners ?? []).map(runner => runner.id))]
  const { data: traces } = useQuery({
    queryKey: ['runner-traces', sessionId, runnerIds.join(',')],
    queryFn: () => Promise.all(runnerIds.map(id => sessionsApi.getRunnerTrace(id))),
    enabled: runnerIds.length > 0,
  })

  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div className="flex items-start gap-3">
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(-1)}>返回</Button>
          <div>
            <Typography.Title level={3} style={{ margin: 0 }}>Execution Timeline</Typography.Title>
            <Typography.Text type="secondary">Session {sessionId?.slice(0, 12)} · {session?.origin_type}</Typography.Text>
          </div>
        </div>
        <Select allowClear placeholder="筛选事件类型" value={eventType} onChange={setEventType}
          options={types.map(type => ({ label: type, value: type }))} style={{ width: 240 }} />
      </div>

      <Card>
        {isLoading ? <div className="py-20 text-center"><Spin /></div> : events.length === 0 ? <Empty description="暂无可见事件" /> : (
          <Timeline items={events.map(event => ({
            color: event.event_type.endsWith('completed') ? 'green' : event.event_type.includes('failed') ? 'red' : 'blue',
            children: (
              <div className="pb-3">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <Tag>#{event.sequence}</Tag><Tag color="blue">{event.event_type}</Tag>
                  <Typography.Text type="secondary">{event.created_at?.replace('T', ' ').slice(0, 19)}</Typography.Text>
                </div>
                <Typography.Paragraph>{event.summary}</Typography.Paragraph>
                <Descriptions size="small" column={2} bordered>
                  <Descriptions.Item label="Run">{event.run_id?.slice(0, 12) || '—'}</Descriptions.Item>
                  <Descriptions.Item label="Event ID">{event.event_id.slice(0, 12)}</Descriptions.Item>
                </Descriptions>
                {Object.keys(event.payload || {}).length > 0 && (
                  <pre className="mt-2 overflow-x-auto rounded-lg bg-muted p-3 text-xs">{JSON.stringify(event.payload, null, 2)}</pre>
                )}
              </div>
            ),
          }))} />
        )}
      </Card>
      <Card className="mt-4" title="Runner Traces" extra={<Typography.Text type="secondary">脱敏、非权威诊断</Typography.Text>}>
        {!traces?.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无 Runner Trace" /> : (
          <Collapse items={traces.map(trace => ({
            key: trace.runner_id,
            label: <span><Tag>{trace.runner_id.slice(0, 12)}</Tag><Tag color={trace.trace?.status === 'completed' ? 'green' : trace.trace?.status === 'failed' ? 'red' : 'default'}>{trace.trace?.status || 'not available'}</Tag></span>,
            children: trace.status === 'not_available' ? <Empty description="Trace 尚不可用" /> : (
              <Timeline items={trace.spans.map(span => ({
                color: span.status === 'ok' ? 'green' : 'red',
                children: <div>
                  <div className="flex flex-wrap items-center gap-2"><Typography.Text strong>{span.name}</Typography.Text><Tag>{span.span_kind}</Tag><Tag>{span.duration_ms} ms</Tag></div>
                  {(span.input_tokens != null || span.output_tokens != null) && <Typography.Text type="secondary">tokens: {span.input_tokens ?? 0} in / {span.output_tokens ?? 0} out</Typography.Text>}
                  {Object.keys(span.attributes || {}).length > 0 && <pre className="mt-2 overflow-x-auto rounded-lg bg-muted p-3 text-xs">{JSON.stringify(span.attributes, null, 2)}</pre>}
                </div>,
              }))} />
            ),
          }))} />
        )}
      </Card>
    </div>
  )
}
