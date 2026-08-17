import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Popover, Badge, Button, Divider, List, Empty, Typography, Tag, Modal, Input, message as toast } from 'antd'
import { Bell } from 'lucide-react'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import { RequiredAction, sessionsApi } from '@/api/sessions'
import { AgentScheduleFire, schedulesApi } from '@/api/schedules'
import { displayLabel } from '@/lib/presentation'

dayjs.extend(relativeTime)

// Web does not manufacture Channel deliveries. This bell projects unresolved
// Required Actions plus completed user-owned schedule fires; durable external
// Channel delivery remains scoped to the originating Channel Session.
export default function NotificationBell() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [selectedClarification, setSelectedClarification] = useState<RequiredAction | null>(null)
  const [answer, setAnswer] = useState('')
  const { data: actions = [] } = useQuery({
    queryKey: ['required-actions'], queryFn: sessionsApi.getRequiredActions, refetchInterval: 10000,
  })
  const { data: sessions = [] } = useQuery({ queryKey: ['sessions'], queryFn: sessionsApi.list })
  const { data: scheduleFires = [] } = useQuery({
    queryKey: ['schedule-notifications'], queryFn: schedulesApi.unreadNotifications,
    refetchInterval: 10000,
  })
  const openAction = (action: RequiredAction) => {
    if (action.type === 'clarification' && action.source_session_owned === false) {
      setOpen(false)
      setSelectedClarification(action)
      setAnswer('')
      return
    }
    const session = sessions.find(item => item.id === action.session_id)
    setOpen(false)
    if (session) navigate(`/agents/${session.entry_agent_id}/chat/${session.id}`)
  }
  const submitClarification = async () => {
    if (!selectedClarification || selectedClarification.type !== 'clarification' || !answer.trim()) return
    try {
      await sessionsApi.answerClarification(selectedClarification, answer.trim())
      setSelectedClarification(null)
      setAnswer('')
      await queryClient.invalidateQueries({ queryKey: ['required-actions'] })
      toast.success('回答已提交，智能体将继续原任务')
    } catch {
      toast.error('回答提交失败，请刷新后重试')
    }
  }
  const actionTitle = (action: RequiredAction) => {
    if ('presentation' in action) return action.presentation.title
    if (action.type === 'clarification') return action.question
    if (action.type === 'tool_confirmation') return action.tool_key
    return '群组操作待处理'
  }
  const openScheduleFire = async (fire: AgentScheduleFire) => {
    setOpen(false)
    await schedulesApi.readNotification(fire.id)
    await queryClient.invalidateQueries({ queryKey: ['schedule-notifications'] })
    navigate(fire.session_id
      ? `/agents/${fire.agent_id}/chat/${fire.session_id}`
      : '/schedules')
  }
  const markScheduleRead = async () => {
    await schedulesApi.readAllNotifications()
    await queryClient.invalidateQueries({ queryKey: ['schedule-notifications'] })
  }
  const count = actions.length + scheduleFires.length
  return <><Popover trigger="click" placement="rightTop" open={open} onOpenChange={setOpen} content={
    <div className="w-80"><div className="px-1 pb-2"><Typography.Text strong>待处理与后台完成</Typography.Text>
      <p className="text-xs text-muted-foreground">审批、澄清和定时任务结果均来自持久化状态。</p></div>
      <List className="max-h-96 overflow-y-auto" dataSource={actions}
        locale={{ emptyText: scheduleFires.length ? null : <Empty description="暂无新事项" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        renderItem={(item: RequiredAction) => <List.Item className="cursor-pointer" onClick={() => openAction(item)}>
          <List.Item.Meta title={<span><Tag color="warning">{displayLabel(item.type)}</Tag>{actionTitle(item)}</span>}
            description={<span className="text-xs">{dayjs('created_at' in item ? item.created_at : new Date()).fromNow()}</span>} />
        </List.Item>} />
      {!!scheduleFires.length && <>
        <Divider className="!my-2" />
        <div className="flex items-center justify-between px-1"><Typography.Text strong>定时任务结果</Typography.Text>
          <Button type="link" size="small" onClick={markScheduleRead}>全部已读</Button></div>
        <List className="max-h-72 overflow-y-auto" dataSource={scheduleFires}
          renderItem={item => <List.Item className="cursor-pointer" onClick={() => void openScheduleFire(item)}>
            <List.Item.Meta title={<span><Tag color={item.status === 'succeeded' ? 'green' : 'red'}>{displayLabel(item.status)}</Tag>{item.schedule_name}</span>}
              description={<span className="text-xs">{item.agent_name} · {dayjs(item.completed_at || item.updated_at).fromNow()}</span>} />
          </List.Item>} />
      </>}
    </div>
  }><button type="button" className="hover:bg-sidebar-accent/60 relative flex size-8 shrink-0 items-center justify-center rounded-lg" title="待处理操作">
    <Bell className="size-4" />{!!count && <Badge count={count} size="small" className="absolute -right-1 -top-1" />}
  </button></Popover>
  <Modal
    open={selectedClarification?.type === 'clarification'}
    title="智能体向你请求补充信息"
    okText="提交回答"
    cancelText="稍后处理"
    okButtonProps={{ disabled: !answer.trim() }}
    onOk={() => void submitClarification()}
    onCancel={() => setSelectedClarification(null)}
  >
    {selectedClarification?.type === 'clarification' && <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        {selectedClarification.requester_display_name || '一位平台用户'} 通过
        {selectedClarification.asked_by_agent_name ? `「${selectedClarification.asked_by_agent_name}」` : '智能体'} 向你提问。
        你的回答会共享给委托人和该智能体，但你不会获得对方会话的访问权限。
      </p>
      {selectedClarification.context_summary && <div className="rounded-lg bg-muted p-3 text-sm">
        {selectedClarification.context_summary}
      </div>}
      <div className="rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm dark:bg-blue-950/20">
        {selectedClarification.question}
      </div>
      <Input.TextArea
        value={answer}
        onChange={event => setAnswer(event.target.value)}
        autoSize={{ minRows: 3, maxRows: 8 }}
        placeholder="输入回答；请勿填写密码、令牌或其他秘密"
      />
    </div>}
  </Modal></>
}
