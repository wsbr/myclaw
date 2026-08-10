import { useEffect, useMemo, useState } from 'react'
import { ChevronDown, Wrench } from 'lucide-react'
import type { LiveToolProgressEvent, ToolInvocationView } from '@/api/sessions'
import { cn } from '@/lib/utils'

interface Props {
  items?: ToolInvocationView[]
  liveItems?: LiveToolProgressEvent[]
  totalCount?: number
  historical?: boolean
  onOpen?: () => void
}

const terminal = new Set(['succeeded', 'failed', 'rejected', 'cancelled', 'outcome_unknown'])

const statusLabel: Record<string, string> = {
  created: '已创建',
  queued: '排队中',
  waiting_confirmation: '等待确认',
  waiting_resource: '等待资源',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  rejected: '已拒绝',
  cancelled: '已取消',
  outcome_unknown: '结果未知',
}

export default function ToolActivityCard({
  items = [], liveItems = [], totalCount, historical = false, onOpen,
}: Props) {
  const merged = useMemo(() => {
    const rows = new Map<string, { id: string; toolKey: string; status: string; summary?: string }>()
    items.forEach(item => rows.set(item.id, {
      id: item.id, toolKey: item.tool_key, status: item.status,
      summary: item.result.summary,
    }))
    liveItems.forEach(item => {
      const id = item.tool_invocation_id || `call:${item.call_id}`
      rows.set(id, {
        ...rows.get(id), id, toolKey: item.tool_key, status: item.status,
      })
      if (item.tool_invocation_id) rows.delete(`call:${item.call_id}`)
    })
    return [...rows.values()]
  }, [items, liveItems])
  const count = Math.max(totalCount || 0, merged.length)
  const allTerminal = merged.length > 0 && merged.every(item => terminal.has(item.status))
  const [open, setOpen] = useState(!historical && !allTerminal)

  useEffect(() => {
    if (historical) return
    if (allTerminal) setOpen(false)
    else if (merged.length) setOpen(true)
  }, [historical, allTerminal, merged.length])

  if (!count) return null

  return <section className="mt-3 overflow-hidden rounded-xl border bg-muted/30 text-xs">
    <button
      type="button"
      className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left"
      onClick={() => {
        const next = !open
        setOpen(next)
        if (next) onOpen?.()
      }}
      aria-expanded={open}
    >
      <span className="flex min-w-0 items-center gap-2 font-medium">
        <Wrench className={cn('size-3.5 shrink-0', !allTerminal && 'animate-pulse text-blue-500')} />
        <span>工具活动 · {count}</span>
      </span>
      <ChevronDown className={cn('size-3.5 transition-transform', open && 'rotate-180')} />
    </button>
    {open && <div className="space-y-2 border-t px-3 py-2">
      {merged.length === 0 && <p className="text-muted-foreground">正在加载工具记录…</p>}
      {merged.map(item => <div key={item.id} className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate font-mono font-medium">{item.toolKey}</p>
          {item.summary && <p className="mt-0.5 line-clamp-2 text-muted-foreground">{item.summary}</p>}
        </div>
        <span className={cn(
          'shrink-0 rounded-full px-2 py-0.5',
          item.status === 'succeeded' && 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300',
          item.status === 'failed' && 'bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300',
          !terminal.has(item.status) && 'bg-blue-100 text-blue-700 dark:bg-blue-950/40 dark:text-blue-300',
        )}>{statusLabel[item.status] || item.status}</span>
      </div>)}
    </div>}
  </section>
}
