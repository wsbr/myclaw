import { useState, useEffect } from 'react'
import { useNavigate, useLocation, Routes, Route, Navigate } from 'react-router-dom'
import {
  Bot,
  ChevronRight,
  Cpu,
  LayoutDashboard,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  Plug,
  Sparkles,
  Users,
  Wrench,
  Zap,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { useAuthStore } from '@/store/auth'
import AgentListPage from '@/pages/Agents/List'
import AgentConfigPage from '@/pages/Agents/Config'
import SessionPage from '@/pages/Session'
import TasksPage from '@/pages/Tasks'
import TracePage from '@/pages/Trace'
import OrchestrationPage from '@/pages/Orchestration'
import UsersPage from '@/pages/Admin/UsersPage'
import ToolsPage from '@/pages/Admin/ToolsPage'
import SkillsPage from '@/pages/Admin/SkillsPage'
import McpPage from '@/pages/Admin/McpPage'
import ModelsPage from '@/pages/Admin/ModelsPage'

const SIDEBAR_KEY = 'myclaw_sidebar_collapsed'

const navItems = [
  { key: '/agents', label: 'Agents', icon: Bot },
  { key: '/tasks', label: 'Tasks', icon: Zap },
  { key: '/users', label: '用户管理', icon: Users },
  { key: '/tools', label: 'Tool 管理', icon: Wrench },
  { key: '/skills', label: 'Skill 管理', icon: Sparkles },
  { key: '/mcp', label: 'MCP 管理', icon: Plug },
  { key: '/models', label: '模型管理', icon: Cpu },
]

const breadcrumbMap: Record<string, string> = {
  '/agents': 'Agents',
  '/tasks': 'Tasks',
  '/users': '用户管理',
  '/tools': 'Tool 管理',
  '/skills': 'Skill 管理',
  '/mcp': 'MCP 管理',
  '/models': '模型管理',
}

function breadcrumbLabel(pathname: string): string {
  if (pathname.startsWith('/agents') && pathname.includes('/config')) return 'Agent 配置'
  if (pathname.startsWith('/agents') && pathname.includes('/chat')) return '对话'
  if (pathname.startsWith('/sessions') && pathname.includes('/trace')) return 'Trace'
  if (pathname.startsWith('/sessions') && pathname.includes('/orchestration')) return '编排图'

  const match = navItems
    .filter((item) => pathname === item.key || pathname.startsWith(`${item.key}/`))
    .sort((a, b) => b.key.length - a.key.length)[0]

  return match ? breadcrumbMap[match.key] ?? match.label : '工作台'
}

function getSelectedKey(pathname: string): string {
  const match = navItems
    .filter((item) => pathname === item.key || pathname.startsWith(`${item.key}/`))
    .sort((a, b) => b.key.length - a.key.length)[0]
  return match?.key ?? '/agents'
}

export default function WorkspaceLayout() {
  const navigate = useNavigate()
  const location = useLocation()
  const { user, logout } = useAuthStore()
  const [collapsed, setCollapsed] = useState(false)

  useEffect(() => {
    setCollapsed(localStorage.getItem(SIDEBAR_KEY) === 'true')
  }, [])

  const toggleSidebar = () => {
    const next = !collapsed
    setCollapsed(next)
    localStorage.setItem(SIDEBAR_KEY, String(next))
  }

  const selectedKey = getSelectedKey(location.pathname)
  const isChatPage = location.pathname.includes('/chat')

  return (
    <div className="flex h-screen w-full bg-background">
      <aside
        className={cn(
          'bg-sidebar text-sidebar-foreground flex shrink-0 flex-col border-r border-sidebar-border transition-[width] duration-200 ease-out',
          collapsed ? 'w-14' : 'w-60',
        )}
      >
        <div className="flex h-12 items-center justify-between border-b border-sidebar-border px-3">
          {!collapsed ? (
            <>
              <span className="text-primary ml-1 cursor-default font-serif text-lg font-medium">
                MyClaw
              </span>
              <Button variant="ghost" size="icon" className="size-8" onClick={toggleSidebar}>
                <PanelLeftClose className="size-4" />
              </Button>
            </>
          ) : (
            <button
              type="button"
              className="group flex size-full items-center justify-center"
              onClick={toggleSidebar}
            >
              <span className="text-primary font-serif text-sm group-hover:hidden">MC</span>
              <PanelLeftOpen className="text-muted-foreground hidden size-4 group-hover:block" />
            </button>
          )}
        </div>

        <nav className="flex flex-1 flex-col gap-1 overflow-y-auto p-2">
          {navItems.map(({ key, label, icon: Icon }) => {
            const active = selectedKey === key
            return (
              <button
                key={key}
                type="button"
                onClick={() => navigate(key)}
                className={cn(
                  'flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors',
                  active
                    ? 'bg-sidebar-accent text-sidebar-accent-foreground font-medium'
                    : 'text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground',
                  collapsed && 'justify-center px-2',
                )}
                title={collapsed ? label : undefined}
              >
                <Icon className="size-4 shrink-0" />
                {!collapsed && <span className="truncate">{label}</span>}
              </button>
            )
          })}
        </nav>

        <div className="border-t border-sidebar-border p-2">
          <div
            className={cn(
              'flex items-center gap-2 rounded-lg px-2 py-2',
              collapsed ? 'justify-center' : 'justify-between',
            )}
          >
            {!collapsed && (
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{user?.username ?? 'User'}</p>
                <p className="text-muted-foreground truncate text-xs">{user?.email}</p>
              </div>
            )}
            <Button
              variant="ghost"
              size="icon"
              className="size-8 shrink-0"
              title="退出登录"
              onClick={() => {
                logout()
                navigate('/login')
              }}
            >
              <LogOut className="size-4" />
            </Button>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        {!isChatPage && (
          <header className="flex h-14 shrink-0 items-center justify-between border-b bg-background/80 px-6 backdrop-blur-sm">
            <div className="flex items-center gap-2 text-sm">
              <LayoutDashboard className="text-muted-foreground size-4" />
              <ChevronRight className="text-muted-foreground size-3" />
              <span className="font-medium">{breadcrumbLabel(location.pathname)}</span>
            </div>
          </header>
        )}

        <main className={cn('min-h-0 flex-1 overflow-auto', isChatPage ? 'p-0' : 'p-6')}>
          <Routes>
            <Route path="/" element={<AgentListPage />} />
            <Route path="/agents" element={<AgentListPage />} />
            <Route path="/agents/new/config" element={<AgentConfigPage />} />
            <Route path="/agents/:id/config" element={<AgentConfigPage />} />
            <Route path="/agents/:agentId/chat" element={<SessionPage />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/users" element={<UsersPage />} />
            <Route path="/tools" element={<ToolsPage />} />
            <Route path="/skills" element={<SkillsPage />} />
            <Route path="/mcp" element={<McpPage />} />
            <Route path="/models" element={<ModelsPage />} />
            <Route path="/admin" element={<Navigate to="/users" replace />} />
            <Route path="/sessions/:sessionId/trace" element={<TracePage />} />
            <Route path="/sessions/:sessionId/orchestration" element={<OrchestrationPage />} />
          </Routes>
        </main>
      </div>
    </div>
  )
}
