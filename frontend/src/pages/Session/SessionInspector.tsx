import { useMemo } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Empty, Modal, Progress, Tabs, Tag, Tooltip, Typography, message } from 'antd'
import {
  Activity, Brain, CheckCircle2, CircleAlert, Download, FileBox, GitBranch,
  Hand, PackageCheck, Play, Trash2, Users, Wrench, X,
} from 'lucide-react'
import type {
  AgentInvocation, Artifact, ExecutionEvent, Run, ToolInvocationView, VerificationResult, WorkingPlan,
} from '@/api/sessions'
import { sessionsApi } from '@/api/sessions'
import { cn } from '@/lib/utils'
import {
  eventPhase, formatClock, formatDuration, friendlyEventLabel, isTerminalRun, phaseMeta,
  runStatusMeta, type RuntimePhase,
} from './runtimeView'

interface Props {
  sessionId: string | null
  runId: string | null
  run?: Run
  plan?: WorkingPlan
  collaborators?: AgentInvocation[]
  onClose: () => void
}

const phaseIcons: Record<RuntimePhase, typeof Activity> = {
  start: Play,
  think: Brain,
  plan: GitBranch,
  tool: Wrench,
  collaborate: Users,
  action: Hand,
  deliver: PackageCheck,
  system: Activity,
}

const phaseClasses: Record<RuntimePhase, string> = {
  start: 'border-slate-300 bg-slate-50 text-slate-600 dark:bg-slate-900',
  think: 'border-blue-300 bg-blue-50 text-blue-600 dark:bg-blue-950',
  plan: 'border-violet-300 bg-violet-50 text-violet-600 dark:bg-violet-950',
  tool: 'border-amber-300 bg-amber-50 text-amber-600 dark:bg-amber-950',
  collaborate: 'border-cyan-300 bg-cyan-50 text-cyan-600 dark:bg-cyan-950',
  action: 'border-orange-300 bg-orange-50 text-orange-600 dark:bg-orange-950',
  deliver: 'border-emerald-300 bg-emerald-50 text-emerald-600 dark:bg-emerald-950',
  system: 'border-slate-300 bg-slate-50 text-slate-600 dark:bg-slate-900',
}

async function loadRunTimeline(runId: string) {
  const items: ExecutionEvent[] = []
  let cursor = 0
  let hasMore = true
  let sessionHighWatermark = 0
  for (let pageNo = 0; hasMore && pageNo < 20; pageNo += 1) {
    const page = await sessionsApi.getRunTimeline(runId, cursor)
    items.push(...page.items)
    sessionHighWatermark = page.session_high_watermark
    hasMore = page.has_more && page.scanned_through_sequence > cursor
    cursor = Math.max(cursor, page.scanned_through_sequence)
  }
  return { items, scanned_through_sequence: cursor, session_high_watermark: sessionHighWatermark, has_more: hasMore }
}

function RuntimeEventItem({ event, last }: { event: ExecutionEvent; last: boolean }) {
  const phase = eventPhase(event.event_type)
  const Icon = phaseIcons[phase]
  return <div className="flex gap-3">
    <div className="flex flex-col items-center">
      <span className={cn('flex size-7 shrink-0 items-center justify-center rounded-full border', phaseClasses[phase])}>
        <Icon className="size-3.5" />
      </span>
      {!last && <span className="my-1 w-px flex-1 bg-border" />}
    </div>
    <details className="group min-w-0 flex-1 pb-4">
      <summary className="cursor-pointer list-none">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-xs font-medium">{friendlyEventLabel(event)}</span>
          <time className="shrink-0 text-[10px] tabular-nums text-muted-foreground">{formatClock(event.created_at)}</time>
        </div>
        <div className="mt-1 flex items-center gap-1.5 text-[10px] text-muted-foreground">
          <span>{phaseMeta[phase].label}</span>
          <span>·</span>
          <span className="truncate font-mono">{event.event_type}</span>
        </div>
      </summary>
      <pre className="mt-2 max-h-48 overflow-auto rounded-lg bg-muted p-2 text-[10px] leading-5 text-muted-foreground">{JSON.stringify(event.payload, null, 2)}</pre>
    </details>
  </div>
}

function ArtifactList({ artifacts, deleting, onDelete }: {
  artifacts: Artifact[]
  deleting: boolean
  onDelete: (artifact: Artifact) => void
}) {
  if (!artifacts.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无产物" />
  return <div className="space-y-3">
    {artifacts.map(artifact => <div key={artifact.id} className="rounded-xl border bg-card p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium">{artifact.name}</p>
          <p className="mt-1 text-xs text-muted-foreground">v{artifact.version_no} · {(artifact.size_bytes / 1024).toFixed(1)} KB</p>
        </div>
        <Tag color={artifact.status === 'published' ? 'green' : artifact.status === 'unavailable' ? 'orange' : 'default'}>{artifact.status}</Tag>
      </div>
      <Typography.Text className="mt-2 block" copyable={{ text: artifact.checksum }} type="secondary" style={{ fontSize: 11 }}>
        {artifact.checksum.slice(0, 16)}…
      </Typography.Text>
      <div className="mt-3 flex gap-1">
        <Button aria-label="下载" size="small" icon={<Download className="size-3.5" />} disabled={artifact.status !== 'published'} onClick={() => sessionsApi.downloadArtifact(artifact)}>下载</Button>
        <Button size="small" danger icon={<Trash2 className="size-3.5" />} disabled={artifact.status === 'deleted'} loading={deleting} onClick={() => onDelete(artifact)}>删除引用</Button>
      </div>
    </div>)}
  </div>
}

function VerificationList({ items }: { items: VerificationResult[] }) {
  if (!items.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前 Run 暂无验证结果" />
  return <div className="space-y-3">
    {items.map(result => {
      const cases = result.observed_facts.filter(item => typeof item.case_id === 'string')
      const passedCases = cases.filter(item => Object.values((item.checks as Record<string, boolean> | undefined) || {}).every(Boolean)).length
      return <div key={result.id} className="rounded-xl border bg-card p-3">
        <div className="flex items-center justify-between gap-2">
          <Typography.Text strong>{result.rule_key}</Typography.Text>
          <Tag color={result.status === 'passed' ? 'green' : result.status === 'failed' ? 'red' : 'orange'}>{result.status}</Tag>
        </div>
        <p className="mt-2 text-xs leading-5 text-muted-foreground">
          epoch {result.verification_epoch} · cases {passedCases}/{cases.length || 9} · evidence {result.source_refs.length}
        </p>
        {result.missing_facts.length > 0 && <details className="mt-2 text-xs text-muted-foreground">
          <summary className="cursor-pointer">查看缺失事实</summary>
          <pre className="mt-2 max-h-40 overflow-auto rounded bg-muted p-2 text-[10px]">{JSON.stringify(result.missing_facts, null, 2)}</pre>
        </details>}
      </div>
    })}
  </div>
}

function ToolLedger({ items }: { items: ToolInvocationView[] }) {
  if (!items.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前 Run 暂无 Tool 调用" />
  return <div className="space-y-3">
    {[...items].reverse().map(item => {
      const citations = (item.result.content_blocks || []).filter(block => block.type === 'citation')
      return <div key={item.id} className="rounded-xl border bg-card p-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0"><p className="font-mono text-xs font-semibold">{item.tool_key}</p><p className="mt-1 text-[10px] text-muted-foreground">ToolVersion v{item.tool_version_no ?? '?'} · {item.tool_version_content_hash?.slice(0, 10) || 'runtime'}</p></div>
          <Tag color={item.status === 'succeeded' ? 'green' : item.status === 'outcome_unknown' ? 'orange' : item.status === 'failed' ? 'red' : 'blue'}>{item.status}</Tag>
        </div>
        <div className="mt-2 grid grid-cols-3 gap-1 text-[10px] text-muted-foreground">
          <span>{item.provider || 'myclaw'}</span><span>{item.attempt_count} attempt</span><span>{String(item.result.timing?.latency_ms ?? item.attempts[item.attempts.length - 1]?.latency_ms ?? '—')} ms</span>
        </div>
        {item.result.summary && <p className="mt-2 text-xs leading-5">{item.result.summary}</p>}
        {citations.length > 0 && <div className="mt-2 space-y-1">{citations.map((citation, index) => <a key={index} className="block truncate text-xs text-blue-600 hover:underline" href={String(citation.url)} target="_blank" rel="noreferrer">{String(citation.title || citation.url)}</a>)}</div>}
        <details className="mt-2 text-xs text-muted-foreground"><summary className="cursor-pointer">脱敏参数、Attempt 与证据</summary><pre className="mt-2 max-h-72 overflow-auto rounded bg-muted p-2 text-[10px]">{JSON.stringify({ arguments: item.arguments, arguments_hash: item.arguments_hash, connection_revision_id: item.connection_revision_id, endpoint: item.endpoint, secret_fingerprint: item.secret_fingerprint, attempts: item.attempts, evidence: item.result.evidence, artifact_ids: item.result.artifact_ids, error: item.result.error }, null, 2)}</pre></details>
      </div>
    })}
  </div>
}

export default function SessionInspector({ sessionId, runId, run, plan, collaborators = [], onClose }: Props) {
  const client = useQueryClient()
  const live = Boolean(run && !isTerminalRun(run.status))
  const { data: artifacts = [] } = useQuery({
    queryKey: ['artifacts', sessionId], queryFn: () => sessionsApi.getArtifacts(sessionId!), enabled: !!sessionId,
    refetchInterval: live ? 2500 : false,
  })
  const { data: timeline } = useQuery({
    queryKey: ['run-timeline', runId], queryFn: () => loadRunTimeline(runId!), enabled: !!runId,
    refetchInterval: live ? 1500 : false,
  })
  const { data: runners = [] } = useQuery({
    queryKey: ['run-runners', runId], queryFn: () => sessionsApi.getRunRunners(runId!), enabled: !!runId,
    refetchInterval: live ? 2000 : false,
  })
  const { data: verifications = [] } = useQuery({
    queryKey: ['verification-results', runId], queryFn: () => sessionsApi.getVerificationResults(runId!), enabled: !!runId,
    refetchInterval: live ? 2500 : false,
  })
  const { data: toolInvocations = [] } = useQuery({
    queryKey: ['tool-invocations', runId], queryFn: () => sessionsApi.getToolInvocations(runId!), enabled: !!runId,
    refetchInterval: live ? 1800 : false,
  })
  const remove = useMutation({
    mutationFn: sessionsApi.deleteArtifact,
    onSuccess: () => client.invalidateQueries({ queryKey: ['artifacts', sessionId] }),
    onError: (error: any) => message.error(error?.response?.data?.error?.code || 'Artifact 删除失败'),
  })

  const events = timeline?.items || []
  const recentEvents = events.slice(-40).reverse()
  const planDone = plan?.items.filter(item => item.status === 'completed').length || 0
  const planProgress = plan?.items.length ? Math.round((planDone / plan.items.length) * 100) : 0
  const toolEvents = events.filter(item => eventPhase(item.event_type) === 'tool').length
  const start = runners.map(item => item.created_at).sort()[0]
  const end = run?.completed_at || undefined
  const status = run ? runStatusMeta[run.status] : null
  const latestMeaningfulEvent = useMemo(() => [...events].reverse().find(item => !['message.created'].includes(item.event_type)), [events])

  const overview = <div className="space-y-4">
    <section className="rounded-2xl border bg-card p-4 shadow-sm">
      {run && status ? <>
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">当前运行</p>
            <div className="mt-2 flex items-center gap-2">
              <span className={cn('size-2.5 rounded-full',
                status.tone === 'active' && 'animate-pulse bg-blue-500',
                status.tone === 'warning' && 'bg-amber-500',
                status.tone === 'success' && 'bg-emerald-500',
                status.tone === 'danger' && 'bg-red-500',
                status.tone === 'neutral' && 'bg-slate-400',
              )} />
              <span className="text-base font-semibold">{status.label}</span>
            </div>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">{status.description}</p>
          </div>
          <Tag>{run.mode.toUpperCase()}</Tag>
        </div>
        <div className="mt-4 grid grid-cols-3 gap-2">
          <div className="rounded-xl bg-muted/55 p-2.5"><p className="text-[10px] text-muted-foreground">持续时间</p><p className="mt-1 text-xs font-semibold tabular-nums">{formatDuration(start, end)}</p></div>
          <div className="rounded-xl bg-muted/55 p-2.5"><p className="text-[10px] text-muted-foreground">事件</p><p className="mt-1 text-xs font-semibold tabular-nums">{events.length}</p></div>
          <div className="rounded-xl bg-muted/55 p-2.5"><p className="text-[10px] text-muted-foreground">工具活动</p><p className="mt-1 text-xs font-semibold tabular-nums">{toolEvents}</p></div>
        </div>
        {latestMeaningfulEvent && <div className="mt-4 rounded-xl border bg-muted/20 p-3">
          <p className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">最近进展</p>
          <p className="mt-1 text-xs font-medium leading-5">{friendlyEventLabel(latestMeaningfulEvent)}</p>
        </div>}
      </> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="发送消息后显示运行状态" />}
    </section>

    {plan && <section className="rounded-2xl border bg-card p-4 shadow-sm">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2"><GitBranch className="size-4 text-violet-500" /><span className="text-sm font-semibold">计划进度</span></div>
        <Tag color="purple">r{plan.revision}</Tag>
      </div>
      <p className="mt-2 line-clamp-2 text-xs leading-5 text-muted-foreground">{plan.summary}</p>
      <Progress className="mb-0 mt-2" percent={planProgress} size="small" strokeColor="#10b981" />
      <p className="mt-1 text-[10px] text-muted-foreground">{planDone}/{plan.items.length} 项完成</p>
    </section>}

    {(runners.length > 0 || collaborators.length > 0) && <section className="rounded-2xl border bg-card p-4 shadow-sm">
      <div className="mb-3 flex items-center gap-2"><Users className="size-4 text-cyan-600" /><span className="text-sm font-semibold">执行单元</span></div>
      <div className="space-y-2">
        {runners.map(item => <div key={item.id} className="flex items-center justify-between gap-2 rounded-lg bg-muted/45 px-3 py-2 text-xs">
          <span className="truncate">Runner {item.id.slice(0, 8)}</span>
          <Tag color={item.status === 'succeeded' ? 'green' : item.status === 'failed' ? 'red' : item.status === 'running' ? 'blue' : 'default'}>{item.status}</Tag>
        </div>)}
        {collaborators.map(item => <div key={item.id} className="flex items-center justify-between gap-2 rounded-lg bg-muted/45 px-3 py-2 text-xs">
          <span className="truncate">Agent {item.target_agent_id.slice(0, 8)}</span>
          <Tag color={item.status === 'succeeded' ? 'green' : item.status === 'failed' ? 'red' : item.status === 'running' ? 'blue' : 'default'}>{item.status}</Tag>
        </div>)}
      </div>
    </section>}

    {artifacts.length > 0 && <section className="rounded-2xl border bg-card p-4 shadow-sm">
      <div className="mb-3 flex items-center gap-2"><FileBox className="size-4 text-emerald-600" /><span className="text-sm font-semibold">最近产物</span></div>
      <div className="space-y-2">
        {artifacts.slice(-3).reverse().map(artifact => <div key={artifact.id} className="flex items-center justify-between gap-2 rounded-lg bg-muted/45 px-3 py-2 text-xs">
          <span className="min-w-0 truncate">{artifact.name}</span>
          <Tag color={artifact.status === 'published' ? 'green' : artifact.status === 'unavailable' ? 'orange' : 'default'}>{artifact.status}</Tag>
        </div>)}
      </div>
    </section>}

    <section>
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2"><Activity className="size-4" /><span className="text-sm font-semibold">运行时间线</span></div>
        <span className="text-[10px] text-muted-foreground">最近 {recentEvents.length} 条</span>
      </div>
      {recentEvents.length ? recentEvents.map((event, index) => <RuntimeEventItem key={event.event_id} event={event} last={index === recentEvents.length - 1} />) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无运行事件" />}
    </section>
  </div>

  return <aside className="absolute inset-y-0 right-0 z-40 flex w-[360px] shrink-0 flex-col border-l bg-background/95 shadow-2xl backdrop-blur-xl xl:static xl:shadow-none">
    <header className="flex h-14 shrink-0 items-center justify-between border-b px-4">
      <div className="flex items-center gap-2"><Activity className="size-4" /><span className="font-semibold">运行监控</span>{live && <span className="runtime-live-badge">LIVE</span>}</div>
      <Tooltip title="关闭运行监控"><Button type="text" size="small" aria-label="关闭运行监控" icon={<X className="size-4" />} onClick={onClose} /></Tooltip>
    </header>
    <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-5">
      <Tabs size="small" defaultActiveKey="overview" items={[
        { key: 'overview', label: '运行', children: overview },
        { key: 'artifacts', label: <span className="inline-flex items-center gap-1"><FileBox className="size-3.5" />产物 {artifacts.length || ''}</span>, children: <ArtifactList artifacts={artifacts} deleting={remove.isPending} onDelete={artifact => Modal.confirm({
          title: '删除 Artifact 引用？',
          content: '删除后该版本将不再可下载；已运行的历史记录与来源信息不会改变。',
          okText: '确认删除', okButtonProps: { danger: true }, cancelText: '保留',
          onOk: () => remove.mutateAsync(artifact),
        })} /> },
        { key: 'tools', label: <span className="inline-flex items-center gap-1"><Wrench className="size-3.5" />Tool {toolInvocations.length || ''}</span>, children: <ToolLedger items={toolInvocations} /> },
        { key: 'verification', label: <span className="inline-flex items-center gap-1">{verifications.some(item => item.status === 'failed') ? <CircleAlert className="size-3.5" /> : <CheckCircle2 className="size-3.5" />}验证 {verifications.length || ''}</span>, children: <VerificationList items={verifications} /> },
      ]} />
    </div>
  </aside>
}
