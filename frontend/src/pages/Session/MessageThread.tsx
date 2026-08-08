import { useEffect, useMemo, useState } from 'react'
import { Button, Spin, Tooltip } from 'antd'
import { ArrowDown, Bot, Check, Copy, UserRound } from 'lucide-react'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import type { Message } from '@/api/sessions'
import { cn } from '@/lib/utils'
import { formatClock } from './runtimeView'

interface Props {
  agentName?: string
  messages: Message[]
  loading: boolean
  running: boolean
  liveText: string
  showScrollButton: boolean
  onScrollToBottom: () => void
}

function MessageItem({ item, agentName }: { item: Message; agentName?: string }) {
  const [copied, setCopied] = useState(false)
  const isUser = item.role === 'user'
  const renderedContent = useMemo(() => {
    if (isUser || item.content_format !== 'markdown') return null
    const html = marked.parse(item.content, { async: false, breaks: true, gfm: true }) as string
    return DOMPurify.sanitize(html)
  }, [isUser, item.content, item.content_format])

  useEffect(() => {
    if (!copied) return
    const timer = window.setTimeout(() => setCopied(false), 1600)
    return () => window.clearTimeout(timer)
  }, [copied])

  return <article className={cn('group flex gap-3', isUser && 'flex-row-reverse')}>
    <div className={cn(
      'mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-xl border shadow-sm',
      isUser ? 'border-primary bg-primary text-primary-foreground' : 'bg-card text-foreground',
    )}>
      {isUser ? <UserRound className="size-4" /> : <Bot className="size-4" />}
    </div>
    <div className={cn('min-w-0 max-w-[84%]', isUser && 'items-end')}>
      <div className={cn('mb-1 flex items-center gap-2 px-1 text-[11px] text-muted-foreground', isUser && 'justify-end')}>
        <span className="font-medium text-foreground/75">{isUser ? '你' : agentName || 'Agent'}</span>
        <time>{formatClock(item.created_at)}</time>
      </div>
      <div className={cn(
        'relative whitespace-pre-wrap break-words px-4 py-3 text-sm leading-7 shadow-sm',
        isUser
          ? 'rounded-2xl rounded-tr-md bg-primary text-primary-foreground'
          : 'rounded-2xl rounded-tl-md border bg-card text-card-foreground',
      )}>
        {renderedContent ? <div
          className="message-markdown"
          onClick={event => {
            const link = (event.target as HTMLElement).closest('a')
            if (!link?.href) return
            event.preventDefault()
            window.open(link.href, '_blank', 'noopener,noreferrer')
          }}
          dangerouslySetInnerHTML={{ __html: renderedContent }}
        /> : item.content}
      </div>
      <div className={cn('mt-1 flex h-7 items-center opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100', isUser && 'justify-end')}>
        <Tooltip title={copied ? '已复制' : '复制消息'}>
          <Button
            type="text"
            size="small"
            aria-label="复制消息"
            icon={copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
            onClick={async () => {
              await navigator.clipboard.writeText(item.content)
              setCopied(true)
            }}
          />
        </Tooltip>
      </div>
    </div>
  </article>
}

export default function MessageThread({
  agentName, messages, loading, running, liveText, showScrollButton, onScrollToBottom,
}: Props) {
  const visibleMessages = messages.filter(item => item.role === 'user' || item.role === 'assistant')

  return <div className="relative">
    {loading && visibleMessages.length === 0 && <div className="py-24 text-center">
      <Spin />
      <p className="mt-3 text-sm text-muted-foreground">正在恢复对话…</p>
    </div>}

    {!loading && visibleMessages.length === 0 && <div className="mx-auto flex max-w-md flex-col items-center py-24 text-center">
      <div className="mb-5 flex size-12 items-center justify-center rounded-2xl border bg-card shadow-sm">
        <Bot className="size-5" />
      </div>
      <p className="text-lg font-semibold">开始与 {agentName || 'Agent'} 协作</p>
      <p className="mt-2 text-sm leading-6 text-muted-foreground">
        发出目标后可以离开页面，任务会继续运行；回到这里即可恢复消息和进度。
      </p>
    </div>}

    <div className="space-y-3">
      {visibleMessages.map(item => <MessageItem key={item.id} item={item} agentName={agentName} />)}
      {running && <article className="flex gap-3" aria-live="polite">
        <div className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-xl border bg-card shadow-sm">
          <Bot className="size-4" />
        </div>
        <div className="min-w-0 max-w-[84%]">
          <div className="mb-1 px-1 text-[11px] font-medium text-foreground/75">{agentName || 'Agent'} · 正在响应</div>
          <div className="min-h-12 whitespace-pre-wrap break-words rounded-2xl rounded-tl-md border bg-card px-4 py-3 text-sm leading-7 shadow-sm">
            {liveText || <div className="flex items-center gap-2 py-1 text-muted-foreground">
              <span className="runtime-pulse-dot" />
              <span className="runtime-pulse-dot [animation-delay:160ms]" />
              <span className="runtime-pulse-dot [animation-delay:320ms]" />
              <span className="ml-1">正在处理当前任务</span>
            </div>}
          </div>
        </div>
      </article>}
    </div>

    {showScrollButton && <Button
      className="sticky bottom-3 left-1/2 z-20 mt-2 -translate-x-1/2 shadow-md"
      shape="round"
      icon={<ArrowDown className="size-3.5" />}
      onClick={onScrollToBottom}
    >回到最新消息</Button>}
  </div>
}
