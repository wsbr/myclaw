import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Popover, Badge, List, Button, Empty, Typography } from 'antd'
import { Bell } from 'lucide-react'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import { notificationsApi, type NotificationItem } from '@/api/notifications'

dayjs.extend(relativeTime)

// 通知落库持久化，弥补 WebSocket Pub/Sub「没人订阅就丢消息」的问题：
// 即使触发通知时对应的对话页面没打开，用户之后也能在这里看到。
export default function NotificationBell() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)

  const { data: unreadCount } = useQuery({
    queryKey: ['notifications', 'unread-count'],
    queryFn: () => notificationsApi.unreadCount(),
    refetchInterval: 15000,
  })

  const { data: items } = useQuery({
    queryKey: ['notifications', 'list'],
    queryFn: () => notificationsApi.list(),
    enabled: open,
  })

  const markReadMutation = useMutation({
    mutationFn: (id: string) => notificationsApi.markRead(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notifications'] })
    },
  })

  const markAllReadMutation = useMutation({
    mutationFn: () => notificationsApi.markAllRead(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notifications'] })
    },
  })

  const handleItemClick = (item: NotificationItem) => {
    if (!item.is_read) markReadMutation.mutate(item.id)
    setOpen(false)
    if (item.agent_id && item.session_id) {
      navigate(`/agents/${item.agent_id}/chat/${item.session_id}`)
    }
  }

  const content = (
    <div className="w-80">
      <div className="flex items-center justify-between px-1 pb-2">
        <Typography.Text strong>通知</Typography.Text>
        <Button
          type="link"
          size="small"
          disabled={!unreadCount}
          onClick={() => markAllReadMutation.mutate()}
        >
          全部已读
        </Button>
      </div>
      <List
        className="max-h-96 overflow-y-auto"
        dataSource={items || []}
        locale={{ emptyText: <Empty description="暂无通知" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        renderItem={(item: NotificationItem) => (
          <List.Item
            className={item.is_read ? 'cursor-pointer opacity-60' : 'cursor-pointer'}
            onClick={() => handleItemClick(item)}
          >
            <List.Item.Meta
              title={item.title || '通知'}
              description={
                <div>
                  <div className="line-clamp-2 text-xs">{item.content}</div>
                  <div className="text-muted-foreground mt-1 text-xs">{dayjs(item.created_at).fromNow()}</div>
                </div>
              }
            />
          </List.Item>
        )}
      />
    </div>
  )

  return (
    <Popover
      trigger="click"
      placement="rightTop"
      open={open}
      onOpenChange={setOpen}
      content={content}
    >
      <button
        type="button"
        className="hover:bg-sidebar-accent/60 relative flex size-8 shrink-0 items-center justify-center rounded-lg"
        title="通知"
      >
        <Bell className="size-4" />
        {!!unreadCount && (
          <Badge
            count={unreadCount}
            size="small"
            className="absolute -right-1 -top-1"
          />
        )}
      </button>
    </Popover>
  )
}
