import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Popover, Badge, List, Empty, Typography, Tag } from 'antd'
import { Bell } from 'lucide-react'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import { RequiredAction, sessionsApi } from '@/api/sessions'
import { displayLabel } from '@/lib/presentation'

dayjs.extend(relativeTime)

// Web does not manufacture Channel deliveries. This bell is only a compact
// projection of unresolved Required Actions; durable channel delivery state
// remains scoped to the originating Channel Session.
export default function NotificationBell() {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const { data: actions = [] } = useQuery({
    queryKey: ['required-actions'], queryFn: sessionsApi.getRequiredActions, refetchInterval: 10000,
  })
  const { data: sessions = [] } = useQuery({ queryKey: ['sessions'], queryFn: sessionsApi.list })
  const openAction = (action: RequiredAction) => {
    const session = sessions.find(item => item.id === action.session_id)
    setOpen(false)
    if (session) navigate(`/agents/${session.entry_agent_id}/chat/${session.id}`)
  }
  const actionTitle = (action: RequiredAction) => {
    if ('presentation' in action) return action.presentation.title
    if (action.type === 'clarification') return action.question
    if (action.type === 'tool_confirmation') return action.tool_key
    return '群组操作待处理'
  }
  return <Popover trigger="click" placement="rightTop" open={open} onOpenChange={setOpen} content={
    <div className="w-80"><div className="px-1 pb-2"><Typography.Text strong>待处理操作</Typography.Text>
      <p className="text-xs text-muted-foreground">来自持久化待处理事项，不代表外部渠道已经送达。</p></div>
      <List className="max-h-96 overflow-y-auto" dataSource={actions}
        locale={{ emptyText: <Empty description="暂无待处理操作" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        renderItem={(item: RequiredAction) => <List.Item className="cursor-pointer" onClick={() => openAction(item)}>
          <List.Item.Meta title={<span><Tag color="warning">{displayLabel(item.type)}</Tag>{actionTitle(item)}</span>}
            description={<span className="text-xs">{dayjs('created_at' in item ? item.created_at : new Date()).fromNow()}</span>} />
        </List.Item>} />
    </div>
  }><button type="button" className="hover:bg-sidebar-accent/60 relative flex size-8 shrink-0 items-center justify-center rounded-lg" title="待处理操作">
    <Bell className="size-4" />{!!actions.length && <Badge count={actions.length} size="small" className="absolute -right-1 -top-1" />}
  </button></Popover>
}
