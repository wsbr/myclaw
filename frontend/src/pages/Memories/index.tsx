import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Card, Empty, Input, Modal, Segmented, Space, Spin, Tag, Typography, message } from 'antd'
import { MemoryFact, memoriesApi } from '@/api/memories'

export default function MemoriesPage() {
  const client = useQueryClient()
  const [status, setStatus] = useState<MemoryFact['status']>('active')
  const [editing, setEditing] = useState<MemoryFact | null>(null)
  const [content, setContent] = useState('')
  const { data = [], isLoading } = useQuery({
    queryKey: ['memories', status], queryFn: () => memoriesApi.list(status),
  })
  const refresh = () => client.invalidateQueries({ queryKey: ['memories'] })
  const edit = useMutation({
    mutationFn: () => memoriesApi.edit(editing!, content.trim()),
    onSuccess: () => { setEditing(null); refresh(); message.success('Memory 已创建新的用户编辑版本') },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || '编辑失败'),
  })
  const remove = useMutation({
    mutationFn: memoriesApi.delete,
    onSuccess: () => { refresh(); message.success('Memory 已删除，后续 Runner 不再召回') },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || '删除失败'),
  })
  return <div className="mx-auto max-w-5xl space-y-4">
    <div className="flex items-center justify-between">
      <div><Typography.Title level={3} style={{ margin: 0 }}>Memory</Typography.Title>
        <Typography.Text type="secondary">只显示属于当前用户的事实版本；平台不允许手工创建任意事实。</Typography.Text></div>
      <Segmented value={status} onChange={value => setStatus(value as MemoryFact['status'])}
        options={[{ label: 'Active', value: 'active' }, { label: 'Superseded', value: 'superseded' }, { label: 'Deleted', value: 'deleted' }]} />
    </div>
    {isLoading ? <div className="py-20 text-center"><Spin /></div> : data.length === 0 ? <Empty description="暂无 Memory" /> :
      data.map(item => <Card key={item.id} size="small" title={<Space><span>{item.fact_key}</span>
        <Tag color={item.scope === 'user_agent' ? 'blue' : 'purple'}>{item.scope}</Tag><Tag>{item.source_kind}</Tag></Space>}
        extra={item.status === 'active' && <Space><Button size="small" onClick={() => { setEditing(item); setContent(item.content) }}>编辑</Button>
          <Button size="small" danger loading={remove.isPending} onClick={() => Modal.confirm({ title: '删除这条 Memory？',
            content: '删除后新 Runner 立即停止召回，历史 Runner 快照保持不变。', okButtonProps: { danger: true },
            onOk: () => remove.mutateAsync(item) })}>删除</Button></Space>}>
        <p className="whitespace-pre-wrap text-sm">{item.content}</p>
        <div className="mt-3 text-xs text-muted-foreground">Head v{item.head_version} · {item.sources.length} 个来源 · {new Date(item.updated_at).toLocaleString()}</div>
        {item.sources.map(source => <div key={source.message_id} className="mt-2 rounded bg-muted px-3 py-2 text-xs">{source.excerpt}</div>)}
      </Card>)}
    <Modal title="编辑 Memory" open={!!editing} onCancel={() => setEditing(null)} okText="保存新版本"
      okButtonProps={{ disabled: !content.trim(), loading: edit.isPending }} onOk={() => edit.mutate()}>
      <Input.TextArea value={content} onChange={event => setContent(event.target.value)} autoSize={{ minRows: 4, maxRows: 12 }} />
      <p className="mt-2 text-xs text-muted-foreground">fact_key 与 scope 不会改变；保存会创建不可变的新版本。</p>
    </Modal>
  </div>
}
