import { useMemo, useState } from 'react'
import { Alert, Button, Empty, Progress, Tag, Typography } from 'antd'
import { Check, ChevronDown, Circle, Clock3, Download, GitBranch, ShieldCheck, Users } from 'lucide-react'
import type {
  AgentInvocation, ApprovalAction, Artifact, Run, UserSession, WorkingPlan,
} from '@/api/sessions'
import { cn } from '@/lib/utils'
import { isTerminalRun, runStatusMeta } from './runtimeView'

interface Props {
  session: UserSession | undefined
  run: Run | undefined
  plan: WorkingPlan | undefined
  collaborators: AgentInvocation[]
  approval: ApprovalAction | undefined
  artifacts: Artifact[]
  busy: boolean
  onApprove: () => Promise<void>
  onReject: (reason?: string) => Promise<void>
  onExit: () => Promise<void>
  onDownload: (artifact: Artifact) => Promise<void>
}

const invocationColor: Record<string, string> = {
  succeeded: 'success', failed: 'error', rejected: 'error', running: 'processing',
  queued: 'warning', created: 'warning', cancelled: 'default', timed_out: 'error',
}

function PlanStep({ item, index, last }: { item: WorkingPlan['items'][number]; index: number; last: boolean }) {
  const completed = item.status === 'completed'
  const active = item.status === 'in_progress'
  const Icon = completed ? Check : active ? Clock3 : Circle

  return <div className="flex gap-3">
    <div className="flex flex-col items-center">
      <span className={cn(
        'flex size-6 items-center justify-center rounded-full border',
        completed && 'border-emerald-500 bg-emerald-500 text-white',
        active && 'border-blue-500 bg-blue-50 text-blue-600 dark:bg-blue-950',
        !completed && !active && 'bg-background text-muted-foreground',
      )}><Icon className={cn('size-3.5', active && 'animate-pulse')} /></span>
      {!last && <span className={cn('my-1 w-px flex-1 bg-border', completed && 'bg-emerald-400/60')} />}
    </div>
    <div className="min-w-0 flex-1 pb-4">
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold text-muted-foreground">步骤 {index + 1}</span>
        {active && <Tag color="processing">进行中</Tag>}
        {completed && <Tag color="success">已完成</Tag>}
      </div>
      <p className={cn('mt-1 text-sm leading-6', completed && 'text-muted-foreground')}>{item.text}</p>
    </div>
  </div>
}

export default function PlanPanel({
  session, run, plan, collaborators, approval, artifacts, busy,
  onApprove, onReject, onExit, onDownload,
}: Props) {
  const [detailsOpen, setDetailsOpen] = useState(true)
  const waitingForDecision = run?.status === 'waiting_approval' && Boolean(approval)
  const readonly = session?.permission_mode === 'plan_readonly'
  const canExit = readonly || !run || isTerminalRun(run.status)
  const progress = useMemo(() => {
    if (!plan?.items.length) return 0
    const completed = plan.items.filter(item => item.status === 'completed').length
    const active = plan.items.some(item => item.status === 'in_progress') ? 0.5 : 0
    return Math.round(((completed + active) / plan.items.length) * 100)
  }, [plan])

  if (!session || session.interaction_mode !== 'plan') {
    return <div className="rounded-2xl border border-dashed bg-muted/20 px-6 py-10 text-center">
      <div className="mx-auto mb-4 flex size-11 items-center justify-center rounded-2xl border bg-card shadow-sm">
        <GitBranch className="size-5" />
      </div>
      <p className="text-base font-semibold">进入 Plan 模式</p>
      <p className="mx-auto mt-2 max-w-lg text-sm leading-6 text-muted-foreground">
        Agent 会先只读调查并维护 WorkingPlan；你批准当前方案后，才会在同一会话中开始实施。
      </p>
    </div>
  }

  const status = run ? runStatusMeta[run.status] : null

  return <section className="overflow-hidden rounded-2xl border bg-card shadow-sm">
    <header className="border-b bg-muted/25 px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <GitBranch className="size-4 text-violet-500" />
            <span className="font-semibold">Plan 权限与执行</span>
            <Tag color={readonly ? 'gold' : 'green'}>{session.permission_mode} · r{session.permission_revision}</Tag>
            {status && <Tag color={status.tone === 'danger' ? 'red' : status.tone === 'success' ? 'green' : status.tone === 'active' ? 'blue' : 'orange'}>{status.label}</Tag>}
          </div>
          <p className="mt-2 text-xs leading-5 text-muted-foreground">
            {readonly ? '当前只读：可以调查、澄清和更新计划，副作用仍被阻断。' : '当前计划已获批准，Agent 正在同一上下文中动态实施。'}
          </p>
        </div>
        <div className="flex items-center gap-1">
          {canExit && <Button size="small" loading={busy} onClick={onExit}>退出 Plan</Button>}
          <Button type="text" size="small" aria-label="展开或收起 Plan" icon={<ChevronDown className={cn('size-4 transition-transform', !detailsOpen && '-rotate-90')} />} onClick={() => setDetailsOpen(value => !value)} />
        </div>
      </div>
    </header>

    {detailsOpen && <div className="space-y-5 p-5">
      <section>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className="font-semibold">WorkingPlan</span>
            {plan && <Tag color="purple">revision {plan.revision}</Tag>}
          </div>
          {plan && <span className="text-xs tabular-nums text-muted-foreground">{plan.items.filter(item => item.status === 'completed').length}/{plan.items.length} 完成</span>}
        </div>
        {!plan ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={run ? 'Agent 正在调查并形成计划…' : '输入目标后开始只读调查'} /> : <>
          <p className="mb-3 text-sm leading-6 text-muted-foreground">{plan.summary}</p>
          <Progress percent={progress} showInfo={false} strokeColor="#10b981" trailColor="var(--muted)" />
          <div className="mt-4">
            {plan.items.map((item, index) => <PlanStep key={`${index}-${item.text}`} item={item} index={index} last={index === plan.items.length - 1} />)}
          </div>
          {plan.explanation && <Alert type="info" showIcon message={plan.explanation} />}
        </>}
      </section>

      <section className={cn('rounded-xl border p-4', waitingForDecision ? 'border-amber-300 bg-amber-50/60 dark:bg-amber-950/15' : 'bg-muted/20')}>
        <div className="mb-2 flex items-center gap-2">
          <ShieldCheck className="size-4 text-amber-600" />
          <span className="font-semibold">Required Action</span>
          {waitingForDecision && <Tag color="warning">需要决定</Tag>}
        </div>
        {waitingForDecision ? <>
          <p className="font-medium">{approval?.presentation.title}</p>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">{approval?.presentation.summary}</p>
          <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <span>Plan revision {approval?.presentation.plan_revision}</span>
            <span>Permission revision {approval?.presentation.permission_revision}</span>
            <Typography.Text copyable={{ text: approval?.subject_hash }} type="secondary" style={{ fontSize: 12 }}>hash {approval?.subject_hash.slice(0, 10)}…</Typography.Text>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <Button type="primary" loading={busy} onClick={onApprove}>批准并在同一 Session 实施</Button>
            <Button danger loading={busy} onClick={() => onReject()}>拒绝当前计划</Button>
          </div>
        </> : <p className="text-sm text-muted-foreground">
          {run?.status === 'waiting_user' ? 'Agent 正在等待你的补充信息。' : '当前没有待处理的 Plan 批准。'}
        </p>}
        {readonly && plan && ['waiting_approval', 'waiting_user', 'completed'].includes(run?.status || '') && <p className="mt-3 text-xs leading-5 text-muted-foreground">
          需要调整时，直接在下方消息框描述修改方向；反馈会作为新消息进入同一 Session，并生成新的 Plan revision。
        </p>}
      </section>

      <div className="grid gap-3 lg:grid-cols-2">
        <section className="rounded-xl border p-4">
          <div className="mb-3 flex items-center gap-2"><Users className="size-4 text-muted-foreground" /><span className="font-semibold">协作者</span><Tag>{collaborators.length}</Tag></div>
          {collaborators.length === 0 ? <p className="text-sm text-muted-foreground">尚未调用协作者。</p> : <div className="space-y-3">
            {collaborators.map(item => <div key={item.id}>
              <div className="flex items-center justify-between gap-2"><Typography.Text code>{item.target_agent_id.slice(0, 10)}</Typography.Text><Tag color={invocationColor[item.status]}>{item.status}</Tag></div>
              <p className="mt-1 line-clamp-2 text-xs leading-5 text-muted-foreground">{item.result_summary || item.request_text || item.error_code}</p>
            </div>)}
          </div>}
        </section>
        <section className="rounded-xl border p-4">
          <div className="mb-3 flex items-center gap-2"><Download className="size-4 text-muted-foreground" /><span className="font-semibold">Artifacts</span><Tag>{artifacts.length}</Tag></div>
          {artifacts.length === 0 ? <p className="text-sm text-muted-foreground">批准实施后，产物会在这里出现。</p> : <div className="space-y-2">
            {artifacts.slice(0, 4).map(artifact => <div key={artifact.id} className="flex items-center justify-between gap-2 text-sm">
              <span className="min-w-0 truncate">{artifact.name}</span>
              <Button size="small" disabled={artifact.status !== 'published'} onClick={() => onDownload(artifact)}>下载</Button>
            </div>)}
          </div>}
        </section>
      </div>
    </div>}
  </section>
}
