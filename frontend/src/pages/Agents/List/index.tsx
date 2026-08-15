import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Input, Segmented } from 'antd'
import {
  Bot,
  Loader2,
  MessageSquare,
  Pencil,
  Plus,
  Search,
} from 'lucide-react'
import { agentsApi, AgentSummary } from '@/api/agents'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { cn } from '@/lib/utils'
import { useAuthStore } from '@/store/auth'
import { displayLabel } from '@/lib/presentation'

const statusColors: Record<string, string> = {
  published: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  draft: 'bg-amber-50 text-amber-700 border-amber-200',
  inactive: 'bg-gray-50 text-gray-600 border-gray-200',
}

export default function AgentListPage() {
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('all')
  const user = useAuthStore(state => state.user)
  const canCreate = Boolean(user?.roles.some(role => role === 'builder' || role === 'admin'))
  const { data: agents, isLoading } = useQuery({
    queryKey: ['agents'],
    queryFn: agentsApi.list,
  })
  const visibleAgents = useMemo(() => {
    const keyword = query.trim().toLocaleLowerCase()
    return (agents ?? []).filter(agent => {
      if (status !== 'all' && agent.status !== status) return false
      if (!keyword) return true
      return `${agent.name} ${agent.description ?? ''}`.toLocaleLowerCase().includes(keyword)
    })
  }, [agents, query, status])

  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Loader2 className="text-muted-foreground size-8 animate-spin" />
      </div>
    )
  }

  return (
    <div className="app-page">
      <div className="page-header">
        <div>
          <h2 className="page-title">智能体</h2>
          <p className="page-description">
            装配、发布和运行智能体，支持对话、规划与群组协作模式
          </p>
        </div>
        {canCreate && <Button onClick={() => navigate('/agents/new/config')}>
          <Plus className="size-4" />
          新建智能体
        </Button>}
      </div>

      {!!agents?.length && <div className="surface-panel flex flex-wrap items-center justify-between gap-3 p-3">
        <Input
          allowClear
          value={query}
          onChange={event => setQuery(event.target.value)}
          prefix={<Search className="size-3.5 text-muted-foreground" />}
          placeholder="按名称或说明搜索智能体"
          className="w-full sm:max-w-sm"
        />
        <div className="flex items-center gap-3">
          <Segmented value={status} onChange={value => setStatus(String(value))} options={[
            { label: '全部', value: 'all' },
            { label: '已发布', value: 'published' },
            { label: '草稿', value: 'draft' },
            { label: '停用', value: 'inactive' },
          ]} />
          <span className="hidden text-xs tabular-nums text-muted-foreground md:inline">{visibleAgents.length} 个结果</span>
        </div>
      </div>}

      {!agents?.length ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-16 text-center">
            <div className="bg-muted mb-4 flex size-12 items-center justify-center rounded-xl">
              <Bot className="text-muted-foreground size-6" />
            </div>
            <p className="font-medium">暂无智能体</p>
            <p className="text-muted-foreground mt-1 text-sm">点击右上角创建你的第一个智能体</p>
            {canCreate && <Button className="mt-4" onClick={() => navigate('/agents/new/config')}>
              <Plus className="size-4" />
              新建智能体
            </Button>}
          </CardContent>
        </Card>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {visibleAgents.map((agent: AgentSummary) => (
            <Card key={agent.id} className="group overflow-hidden transition-all duration-200 hover:-translate-y-0.5 hover:border-primary/25 hover:shadow-lg">
              <CardHeader className="pb-2">
                <div className="flex items-start gap-3">
                  <div className="bg-primary/10 text-primary flex size-9 shrink-0 items-center justify-center rounded-lg">
                    <Bot className="size-5" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <CardTitle className="truncate">{agent.name}</CardTitle>
                    <Badge
                      variant="outline"
                      className={cn('mt-1', statusColors[agent.status] ?? statusColors.draft)}
                    >
                      {displayLabel(agent.status)}
                    </Badge>
                  </div>
                </div>
                {agent.description && (
                  <CardDescription className="mt-2 line-clamp-2">
                    {agent.description}
                  </CardDescription>
                )}
              </CardHeader>

              <CardContent className="flex flex-wrap gap-1.5 pt-0 pb-2">
                <Badge variant="outline">v{agent.latest_published_version_no ?? '—'}</Badge>
                <Badge variant="outline">{displayLabel(agent.visibility)}</Badge>
                {agent.group_enabled && <Badge variant="outline" className="border-violet-200 bg-violet-50 text-violet-700">群组协作</Badge>}
              </CardContent>

              <CardFooter className="gap-2 pt-2">
                <Button
                  size="sm"
                  className="flex-1"
                  disabled={agent.status !== 'published'}
                  onClick={() => navigate(`/agents/${agent.id}/chat`)}
                >
                  <MessageSquare className="size-3.5" />
                  对话
                </Button>
                {(user?.roles.includes('admin') || (
                  user?.roles.includes('builder') && agent.owner_account_id === user.id
                )) && <Button
                  size="icon"
                  variant="ghost"
                  className="size-8"
                  title="配置"
                  onClick={() => navigate(`/agents/${agent.id}/config`)}
                >
                  <Pencil className="size-3.5" />
                </Button>}
              </CardFooter>
            </Card>
          ))}
        </div>
      )}
      {!!agents?.length && !visibleAgents.length && <Card className="border-dashed">
        <CardContent className="flex flex-col items-center py-14 text-center">
          <Search className="mb-3 size-6 text-muted-foreground" />
          <p className="font-medium">没有匹配的智能体</p>
          <p className="mt-1 text-sm text-muted-foreground">换一个关键词或状态试试</p>
        </CardContent>
      </Card>}
    </div>
  )
}
