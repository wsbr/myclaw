import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Card, Empty, Input, Modal, Segmented, Space, Spin, Tag, Typography, message } from 'antd'
import { MemoryFact, memoriesApi } from '@/api/memories'
import { displayLabel } from '@/lib/presentation'

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
    onSuccess: () => { setEditing(null); refresh(); message.success('记忆已创建新的用户编辑版本') },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || '编辑失败'),
  })
  const remove = useMutation({
    mutationFn: memoriesApi.delete,
    onSuccess: () => { refresh(); message.success('记忆已删除，后续任务不再召回') },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || '删除失败'),
  })
  return <div className="app-page max-w-5xl">
    <div className="flex items-center justify-between">
      <div><Typography.Title level={2} style={{ margin: 0 }}>记忆</Typography.Title>
        <Typography.Text type="secondary">只显示属于当前用户的事实版本；平台不允许手工创建任意事实。</Typography.Text></div>
      <Segmented value={status} onChange={value => setStatus(value as MemoryFact['status'])}
        options={[{ label: '有效', value: 'active' }, { label: '已替代', value: 'superseded' }, { label: '已删除', value: 'deleted' }]} />
    </div>
    {isLoading ? <div className="py-20 text-center"><Spin /></div> : data.length === 0 ? <Empty description="暂无记忆" /> :
      data.map(item => <Card key={item.id} size="small" title={<Space><span>{item.fact_key}</span>
        <Tag color={item.scope === 'user_agent' ? 'blue' : 'purple'}>{displayLabel(item.scope)}</Tag><Tag>{displayLabel(item.source_kind)}</Tag></Space>}
        extra={item.status === 'active' && <Space><Button size="small" onClick={() => { setEditing(item); setContent(item.content) }}>编辑</Button>
          <Button size="small" danger loading={remove.isPending} onClick={() => Modal.confirm({ title: '删除这条记忆？',
            content: '删除后新任务立即停止召回，历史执行快照保持不变。', okButtonProps: { danger: true },
            onOk: () => remove.mutateAsync(item) })}>删除</Button></Space>}>
        <p className="whitespace-pre-wrap text-sm">{item.content}</p>
        <div className="mt-3 text-xs text-muted-foreground">当前版本 v{item.head_version} · {item.sources.length} 个来源 · {new Date(item.updated_at).toLocaleString()}</div>
        {item.sources.map(source => <div key={source.message_id} className="mt-2 rounded bg-muted px-3 py-2 text-xs">{source.excerpt}</div>)}
      </Card>)}
    <Modal title="编辑记忆" open={!!editing} onCancel={() => setEditing(null)} okText="保存新版本"
      okButtonProps={{ disabled: !content.trim(), loading: edit.isPending }} onOk={() => edit.mutate()}>
      <Input.TextArea value={content} onChange={event => setContent(event.target.value)} autoSize={{ minRows: 4, maxRows: 12 }} />
      <p className="mt-2 text-xs text-muted-foreground">事实键与作用域不会改变；保存会创建不可变的新版本。</p>
    </Modal>
  </div>
}
