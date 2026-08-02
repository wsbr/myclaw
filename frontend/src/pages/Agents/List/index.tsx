import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  Bot,
  Loader2,
  MessageSquare,
  Pencil,
  Plus,
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

const statusColors: Record<string, string> = {
  published: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  draft: 'bg-amber-50 text-amber-700 border-amber-200',
  inactive: 'bg-gray-50 text-gray-600 border-gray-200',
}

export default function AgentListPage() {
  const navigate = useNavigate()
  const user = useAuthStore(state => state.user)
  const canCreate = Boolean(user?.roles.some(role => role === 'builder' || role === 'admin'))
  const { data: agents, isLoading } = useQuery({
    queryKey: ['agents'],
    queryFn: agentsApi.list,
  })

  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Loader2 className="text-muted-foreground size-8 animate-spin" />
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-6xl">
      <div className="mb-8 flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight">Agents</h2>
          <p className="text-muted-foreground mt-1 text-sm">
            装配和管理你的智能体，支持 Chat / Plan / Group 模式
          </p>
        </div>
        {canCreate && <Button onClick={() => navigate('/agents/new/config')}>
          <Plus className="size-4" />
          新建 Agent
        </Button>}
      </div>

      {!agents?.length ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-16 text-center">
            <div className="bg-muted mb-4 flex size-12 items-center justify-center rounded-xl">
              <Bot className="text-muted-foreground size-6" />
            </div>
            <p className="font-medium">暂无 Agent</p>
            <p className="text-muted-foreground mt-1 text-sm">点击右上角创建你的第一个智能体</p>
            {canCreate && <Button className="mt-4" onClick={() => navigate('/agents/new/config')}>
              <Plus className="size-4" />
              新建 Agent
            </Button>}
          </CardContent>
        </Card>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {agents.map((agent: AgentSummary) => (
            <Card key={agent.id} className="group">
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
                      {agent.status}
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
                <Badge variant="outline">{agent.visibility}</Badge>
                {agent.group_enabled && <Badge variant="outline">Group</Badge>}
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
    </div>
  )
}
