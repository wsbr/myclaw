import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Empty, Modal, Tabs, Tag, Timeline, Typography, message } from 'antd'
import { DownloadOutlined, DeleteOutlined } from '@ant-design/icons'
import { Artifact, sessionsApi } from '@/api/sessions'

export default function SessionInspector({ sessionId }: { sessionId: string | null }) {
  const client = useQueryClient()
  const { data: artifacts = [] } = useQuery({
    queryKey: ['artifacts', sessionId], queryFn: () => sessionsApi.getArtifacts(sessionId!), enabled: !!sessionId,
  })
  const { data: events } = useQuery({
    queryKey: ['session-inspector-events', sessionId], queryFn: () => sessionsApi.getEvents(sessionId!, 0), enabled: !!sessionId,
  })
  const remove = useMutation({
    mutationFn: sessionsApi.deleteArtifact,
    onSuccess: () => client.invalidateQueries({ queryKey: ['artifacts', sessionId] }),
    onError: (error: any) => message.error(error?.response?.data?.error?.code || 'Artifact 删除失败'),
  })
  const artifactItems = artifacts.length ? artifacts.map((artifact: Artifact) => <div key={artifact.id} className="mb-3 rounded-lg border p-3">
    <div className="flex items-start justify-between gap-2"><div className="min-w-0"><p className="truncate text-sm font-medium">{artifact.name}</p>
      <p className="text-xs text-muted-foreground">v{artifact.version_no} · {(artifact.size_bytes / 1024).toFixed(1)} KB</p></div>
      <Tag color={artifact.status === 'published' ? 'green' : artifact.status === 'unavailable' ? 'orange' : 'default'}>{artifact.status}</Tag></div>
    <Typography.Text className="mt-2 block" copyable={{ text: artifact.checksum }} type="secondary" style={{ fontSize: 11 }}>{artifact.checksum.slice(0, 16)}…</Typography.Text>
    <div className="mt-2 flex gap-1"><Button size="small" icon={<DownloadOutlined />} disabled={artifact.status !== 'published'}
      onClick={() => sessionsApi.downloadArtifact(artifact)}>下载</Button>
      <Button size="small" danger icon={<DeleteOutlined />} disabled={artifact.status === 'deleted'} loading={remove.isPending}
        onClick={() => Modal.confirm({
          title: '删除 Artifact 引用？',
          content: '删除后该版本将不再可下载；已运行 Runner 的历史记录与来源信息不会改变。',
          okText: '确认删除', okButtonProps: { danger: true }, cancelText: '保留',
          onOk: () => remove.mutateAsync(artifact),
        })}>删除引用</Button></div>
  </div>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无 Artifact" />
  const timelineItems = (events?.items || []).slice(-30).reverse().map(event => ({
    color: event.event_type.includes('failed') ? 'red' : event.event_type.includes('completed') ? 'green' : 'blue',
    children: <div><p className="text-xs font-medium">{event.summary}</p><p className="text-[11px] text-muted-foreground">#{event.sequence} · {event.event_type}</p></div>,
  }))
  return <aside className="w-80 shrink-0 overflow-y-auto border-l bg-muted/20 p-3">
    <Tabs size="small" items={[
      { key: 'artifacts', label: `Artifacts (${artifacts.length})`, children: artifactItems },
      { key: 'timeline', label: 'Timeline', children: timelineItems.length ? <Timeline items={timelineItems} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} /> },
    ]} />
  </aside>
}
