import type { ExecutionEvent, Run } from '@/api/sessions'

export const TERMINAL_RUN_STATUSES: Run['status'][] = ['completed', 'cancelled', 'failed']

export const isTerminalRun = (status?: Run['status']) => Boolean(status && TERMINAL_RUN_STATUSES.includes(status))

export const runStatusMeta: Record<Run['status'], {
  label: string
  tone: 'neutral' | 'active' | 'warning' | 'success' | 'danger'
  description: string
}> = {
  active: { label: '正在执行', tone: 'active', description: '智能体正在处理当前任务' },
  waiting_user: { label: '等待回复', tone: 'warning', description: '需要你补充信息后继续' },
  waiting_approval: { label: '等待批准', tone: 'warning', description: '需要你确认下一步操作' },
  waiting_tool: { label: '等待工具', tone: 'warning', description: '正在等待工具资源或结果' },
  waiting_agent: { label: '协作中', tone: 'active', description: '正在等待协作者返回结果' },
  completed: { label: '已完成', tone: 'success', description: '本次运行已经完成' },
  cancelled: { label: '已取消', tone: 'neutral', description: '本次运行已由用户或系统取消' },
  failed: { label: '运行失败', tone: 'danger', description: '运行遇到错误，请查看事件详情' },
}

export type RuntimePhase = 'start' | 'think' | 'plan' | 'tool' | 'collaborate' | 'action' | 'deliver' | 'system'

export const phaseMeta: Record<RuntimePhase, { label: string; tone: string }> = {
  start: { label: '启动', tone: 'slate' },
  think: { label: '分析', tone: 'blue' },
  plan: { label: '计划', tone: 'violet' },
  tool: { label: '工具', tone: 'amber' },
  collaborate: { label: '协作', tone: 'cyan' },
  action: { label: '待办', tone: 'orange' },
  deliver: { label: '交付', tone: 'emerald' },
  system: { label: '系统', tone: 'slate' },
}

export function eventPhase(eventType: string): RuntimePhase {
  if (eventType === 'run.created' || eventType.startsWith('runner.') || eventType === 'work_item.created') return 'start'
  if (eventType.startsWith('agent_turn.') || eventType.startsWith('work_item.running')) return 'think'
  if (eventType.startsWith('working_plan.') || eventType.startsWith('plan.')) return 'plan'
  if (eventType.startsWith('tool_') || eventType.startsWith('tool.') || eventType.startsWith('tool_confirmation.')) return 'tool'
  if (eventType.startsWith('agent_invocation.') || eventType.startsWith('group.')) return 'collaborate'
  if (eventType.startsWith('approval.') || eventType.startsWith('clarification.')) return 'action'
  if (eventType.startsWith('message.') || eventType.startsWith('artifact.') || eventType.startsWith('verification.') || eventType === 'run.completed') return 'deliver'
  return 'system'
}

const EVENT_LABELS: Record<string, string> = {
  'run.created': '运行已创建',
  'run.waiting': '运行暂停等待',
  'run.resumed': '运行已恢复',
  'run.cancellation_requested': '已请求取消',
  'run.completed': '运行完成',
  'run.failed': '运行失败',
  'run.cancelled': '运行已取消',
  'runner.created': '执行器已创建',
  'runner.started': '执行器已启动',
  'runner.succeeded': '执行器完成',
  'runner.failed': '执行器失败',
  'agent_turn.started': '智能体开始分析',
  'agent_turn.waiting_actions': '等待动作结果',
  'agent_turn.completed': '智能体完成本轮',
  'agent_turn.failed': '智能体本轮失败',
  'message.created': '消息已写入',
  'working_plan.updated': '计划已更新',
  'plan.permission_entered': '进入只读计划',
  'plan.permission_exited': '退出计划权限',
  'plan.implementation_approved': '计划已批准实施',
  'approval.requested': '请求批准',
  'approval.approved': '操作已批准',
  'approval.rejected': '操作已拒绝',
  'clarification.requested': '请求补充信息',
  'clarification.answered': '补充信息已提交',
  'tool_invocation.created': '工具调用已创建',
  'tool_invocation.queued': '工具调用排队中',
  'tool_invocation.running': '工具正在执行',
  'tool_invocation.succeeded': '工具执行完成',
  'tool_invocation.failed': '工具执行失败',
  'tool.succeeded': '工具执行完成',
  'tool.failed': '工具执行失败',
  'tool.outcome_unknown': '工具结果未知',
  'agent_invocation.created': '协作请求已创建',
  'agent_invocation.running': '协作者处理中',
  'agent_invocation.succeeded': '协作者已返回',
  'agent_invocation.failed': '协作者执行失败',
  'artifact.published': '产物已发布',
  'verification.completed': '验证已完成',
}

export const friendlyEventLabel = (event: Pick<ExecutionEvent, 'event_type' | 'summary'>) =>
  EVENT_LABELS[event.event_type] || event.summary || event.event_type.split('_').join(' ')

function parseApiTimestamp(value: string) {
  // The API serializes UTC datetimes without an offset. Browsers otherwise
  // interpret those values as local time, which inflates live run durations.
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`
  return new Date(normalized)
}

export function formatClock(value?: string | null) {
  if (!value) return '—'
  const date = parseApiTimestamp(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}

export function formatDuration(start?: string | null, end?: string | null) {
  if (!start) return '—'
  const from = parseApiTimestamp(start).getTime()
  const to = end ? parseApiTimestamp(end).getTime() : Date.now()
  if (!Number.isFinite(from) || !Number.isFinite(to)) return '—'
  const seconds = Math.max(0, Math.round((to - from) / 1000))
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`
}
