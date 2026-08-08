import type { ReactNode } from 'react'
import { Button, Input } from 'antd'
import { Send, Square } from 'lucide-react'

type Mode = 'chat' | 'plan' | 'group'

interface Props {
  mode: Mode
  value: string
  disabled: boolean
  submitting: boolean
  running: boolean
  planEntered: boolean
  hasPlan: boolean
  planCanRevise: boolean
  groupReady: boolean
  accessory?: ReactNode
  onChange: (value: string) => void
  onSubmit: () => void
  onStop: () => void
}

const modeCopy: Record<Mode, { label: string; hint: string }> = {
  chat: { label: 'Chat', hint: '直接讨论并执行' },
  plan: { label: 'Plan', hint: '先调查和对齐，再批准实施' },
  group: { label: 'Group', hint: '向协作组派发工作' },
}

export default function SessionComposer({
  mode, value, disabled, submitting, running, planEntered, hasPlan, planCanRevise, groupReady, accessory,
  onChange, onSubmit, onStop,
}: Props) {
  const placeholder = mode === 'plan'
    ? (planEntered && hasPlan && planCanRevise
      ? '继续讨论或修改方向，例如：先验证迁移回滚，再实现 UI'
      : planEntered ? '当前 Plan 正在实施或已完成；退出 Plan 后可以继续对话' : '描述需要只读调查与规划的长任务目标')
    : mode === 'group'
      ? (groupReady ? '发送给 Reporter 或所选成员' : '请先从 Agent Network 选择成员建立 Group')
      : '输入消息；Enter 发送，Shift+Enter 换行'
  const buttonLabel = mode === 'plan'
    ? (planEntered && hasPlan && planCanRevise ? '修改方向' : planEntered ? '请先退出 Plan' : '进入只读 Plan')
    : mode === 'group' ? '提交 Group 工作' : '发送'

  return <div className="rounded-2xl border bg-card p-2 shadow-[0_12px_36px_-18px_rgba(0,0,0,0.28)]">
    <div className="flex items-center justify-between gap-3 px-2 pb-2 pt-1 text-[11px] text-muted-foreground">
      <div className="flex items-center gap-2">
        <span className="rounded-md bg-muted px-2 py-0.5 font-semibold text-foreground">{modeCopy[mode].label}</span>
        <span>{modeCopy[mode].hint}</span>
      </div>
      <span className="hidden sm:inline">Enter 发送 · Shift+Enter 换行</span>
    </div>
    <div className="flex items-end gap-2">
      {accessory}
      <Input.TextArea
        className="session-composer-textarea"
        value={value}
        onChange={event => onChange(event.target.value)}
        autoSize={{ minRows: 1, maxRows: 7 }}
        placeholder={placeholder}
        disabled={disabled}
        aria-label="消息输入"
        onPressEnter={event => {
          if (!event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault()
            if (value.trim() && !disabled) onSubmit()
          }
        }}
      />
      {running ? <Button
        danger
        className="h-10 shrink-0"
        icon={<Square className="size-3.5 fill-current" />}
        onClick={onStop}
      >停止</Button> : <Button
        type="primary"
        className="h-10 shrink-0"
        icon={<Send className="size-3.5" />}
        loading={submitting}
        disabled={!value.trim() || disabled}
        onClick={onSubmit}
      >{buttonLabel}</Button>}
    </div>
  </div>
}
