import { Input, Button, Tag } from 'antd'
import { CircleHelp, ShieldCheck } from 'lucide-react'
import type { ApprovalAction, ClarificationAction, ToolConfirmation } from '@/api/sessions'

interface Props {
  toolActions: Array<ApprovalAction | ToolConfirmation>
  clarifications: ClarificationAction[]
  answers: Record<string, string>
  deciding: boolean
  answering: boolean
  onAnswerChange: (id: string, value: string) => void
  onDecision: (action: ApprovalAction | ToolConfirmation, decision: 'approve' | 'reject') => void
  onAnswer: (action: ClarificationAction, answer: string) => void
}

export default function PendingActions({
  toolActions, clarifications, answers, deciding, answering,
  onAnswerChange, onDecision, onAnswer,
}: Props) {
  if (!toolActions.length && !clarifications.length) return null

  return <section className="space-y-3" aria-label="待处理事项">
    <div className="flex items-center gap-2 px-1">
      <span className="text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">需要你的操作</span>
      <Tag color="orange">{toolActions.length + clarifications.length}</Tag>
    </div>

    {toolActions.map(action => <div key={action.id} className="overflow-hidden rounded-2xl border border-amber-300/70 bg-amber-50/70 shadow-sm dark:bg-amber-950/15">
      <div className="flex items-start gap-3 p-4">
        <div className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300">
          <ShieldCheck className="size-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Tag color="warning">需要确认</Tag>
            <span className="font-medium">{action.tool_key || ('presentation' in action ? action.presentation.title : '') || '受保护操作'}</span>
          </div>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            Agent 请求执行以下精确参数。批准只对本次参数哈希有效。
          </p>
          {action.arguments && <details className="mt-3 rounded-xl border bg-background/70">
            <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-muted-foreground">查看请求参数</summary>
            <pre className="max-h-48 overflow-auto border-t p-3 text-xs leading-5">{JSON.stringify(action.arguments, null, 2)}</pre>
          </details>}
          <div className="mt-4 flex gap-2">
            <Button aria-label="批准" type="primary" loading={deciding} onClick={() => onDecision(action, 'approve')}>批准</Button>
            <Button aria-label="拒绝" danger loading={deciding} onClick={() => onDecision(action, 'reject')}>拒绝</Button>
          </div>
        </div>
      </div>
    </div>)}

    {clarifications.map(action => <div key={action.id} className="overflow-hidden rounded-2xl border border-blue-300/70 bg-blue-50/60 shadow-sm dark:bg-blue-950/15">
      <div className="flex items-start gap-3 p-4">
        <div className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300">
          <CircleHelp className="size-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2"><Tag color="blue">需要补充信息</Tag><span className="font-medium">Agent 提问</span></div>
          <p className="mt-3 whitespace-pre-wrap text-sm leading-6">{action.question}</p>
          <Input.TextArea
            className="mt-3"
            value={answers[action.id] || ''}
            onChange={event => onAnswerChange(action.id, event.target.value)}
            autoSize={{ minRows: 2, maxRows: 6 }}
            placeholder="输入回答；只会恢复提出该问题的 Work Item"
          />
          <Button
            className="mt-3"
            type="primary"
            loading={answering}
            disabled={!answers[action.id]?.trim()}
            onClick={() => onAnswer(action, answers[action.id].trim())}
          >提交回答</Button>
        </div>
      </div>
    </div>)}
  </section>
}
