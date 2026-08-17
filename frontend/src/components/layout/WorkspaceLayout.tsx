import { lazy, Suspense, useState, useEffect } from 'react'
import { useNavigate, useLocation, Routes, Route, Navigate } from 'react-router-dom'
import {
  Activity,
  Bot,
  Brain,
  BookOpen,
  CalendarClock,
  Coins,
  ChevronRight,
  Cpu,
  LayoutDashboard,
  LogOut,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  ScrollText,
  ShieldCheck,
  Sparkles,
  UserRound,
  Users,
  Wrench,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { useAuthStore } from '@/store/auth'
import NotificationBell from '@/components/layout/NotificationBell'

const AgentListPage = lazy(() => import('@/pages/Agents/List'))
const AgentConfigPage = lazy(() => import('@/pages/Agents/Config'))
const SessionPage = lazy(() => import('@/pages/Session'))
const TracePage = lazy(() => import('@/pages/Trace'))
const UsersPage = lazy(() => import('@/pages/Admin/UsersPage'))
const ToolsPage = lazy(() => import('@/pages/Admin/ToolsPage'))
const SkillsPage = lazy(() => import('@/pages/Admin/SkillsPage'))
const ModelsPage = lazy(() => import('@/pages/Admin/ModelsPage'))
const GovernancePage = lazy(() => import('@/pages/Admin/GovernancePage'))
const AuditPage = lazy(() => import('@/pages/Admin/AuditPage'))
const RuntimePage = lazy(() => import('@/pages/Admin/RuntimePage'))
const UsagePage = lazy(() => import('@/pages/Admin/UsagePage'))
const MemoriesPage = lazy(() => import('@/pages/Memories'))
const KnowledgePage = lazy(() => import('@/pages/Knowledge'))
const AccountPage = lazy(() => import('@/pages/Account'))
const AgentNetworkPage = lazy(() => import('@/pages/AgentNetwork'))
const SchedulesPage = lazy(() => import('@/pages/Schedules'))

const SIDEBAR_KEY = 'myclaw_sidebar_collapsed'

type NavItem = { key: string; label: string; icon: typeof Bot; roles?: string[] }

const navSections: Array<{ label: string; items: NavItem[] }> = [
  { label: '智能体工作区', items: [
    { key: '/agents', label: '智能体', icon: Bot },
    { key: '/network', label: '协作网络', icon: Network },
    { key: '/knowledge', label: '知识库', icon: BookOpen },
    { key: '/schedules', label: '定时任务', icon: CalendarClock },
  ] },
  { label: '个人空间', items: [
    { key: '/memories', label: '记忆', icon: Brain },
    { key: '/account', label: '我的账号', icon: UserRound },
  ] },
  { label: '平台管理', items: [
    { key: '/users', label: '用户管理', icon: Users, roles: ['admin'] },
    { key: '/tools', label: '工具管理', icon: Wrench, roles: ['admin'] },
    { key: '/skills', label: '技能管理', icon: Sparkles, roles: ['builder', 'admin'] },
    { key: '/models', label: '模型管理', icon: Cpu, roles: ['admin'] },
    { key: '/governance', label: '策略与沙箱', icon: ShieldCheck, roles: ['admin'] },
    { key: '/audit', label: '平台审计', icon: ScrollText, roles: ['admin'] },
    { key: '/runtime', label: '运行控制台', icon: Activity, roles: ['admin'] },
    { key: '/usage', label: '用量与成本', icon: Coins, roles: ['admin'] },
  ] },
]

const navItems = navSections.flatMap(section => section.items)

const breadcrumbMap: Record<string, string> = {
  '/agents': '智能体',
  '/network': '协作网络',
  '/memories': '记忆',
  '/knowledge': '知识库',
  '/schedules': '定时任务',
  '/account': '我的账号',
  '/users': '用户管理',
  '/tools': '工具管理',
  '/skills': '技能管理',
  '/models': '模型管理',
  '/governance': '策略与沙箱',
  '/audit': '平台审计',
  '/runtime': '运行控制台',
  '/usage': '用量与成本',
}

function breadcrumbLabel(pathname: string): string {
  if (pathname.startsWith('/agents') && pathname.includes('/config')) return '智能体配置'
  if (pathname.startsWith('/agents') && pathname.includes('/chat')) return '对话'
  if (pathname.startsWith('/sessions') && pathname.includes('/trace')) return '执行轨迹'

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
  const visibleNavSections = navSections.map(section => ({
    ...section,
    items: section.items.filter(item => !item.roles || item.roles.includes(user?.role || 'user')),
  })).filter(section => section.items.length > 0)

  return (
    <div className="flex h-screen w-full bg-background">
      <aside
        className={cn(
          'bg-sidebar text-sidebar-foreground relative z-20 flex shrink-0 flex-col border-r border-sidebar-border transition-[width] duration-200 ease-out',
          collapsed ? 'w-16' : 'w-64',
        )}
      >
        <div className="flex h-16 items-center justify-between border-b border-sidebar-border/80 px-3">
          {!collapsed ? (
            <>
              <div className="flex items-center gap-2.5 pl-1">
                <span className="flex size-8 items-center justify-center rounded-xl bg-primary text-primary-foreground shadow-sm">
                  <Bot className="size-4.5" />
                </span>
                <div>
                  <p className="font-serif text-lg font-semibold leading-5 text-foreground">MyClaw</p>
                  <p className="text-[10px] font-medium tracking-[0.14em] text-muted-foreground">智能体云平台</p>
                </div>
              </div>
              <Button variant="ghost" size="icon" className="size-8" title="收起导航" onClick={toggleSidebar}>
                <PanelLeftClose className="size-4" />
              </Button>
            </>
          ) : (
            <button
              type="button"
              className="group flex size-full items-center justify-center"
              onClick={toggleSidebar}
            >
              <Bot className="size-4 text-primary group-hover:hidden" />
              <PanelLeftOpen className="text-muted-foreground hidden size-4 group-hover:block" />
            </button>
          )}
        </div>

        <nav className="flex flex-1 flex-col overflow-y-auto px-2 py-3">
          {visibleNavSections.map((section, sectionIndex) => <div key={section.label} className={cn(sectionIndex > 0 && 'mt-4')}>
            {!collapsed && <p className="mb-1.5 px-3 text-[10px] font-semibold tracking-[0.14em] text-muted-foreground/75">{section.label}</p>}
            <div className="space-y-1">
              {section.items.map(({ key, label, icon: Icon }) => {
                const active = selectedKey === key
                return (
                  <button
                    key={key}
                    type="button"
                    onClick={() => navigate(key)}
                    className={cn(
                      'group relative flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition-all',
                      active
                        ? 'bg-sidebar-accent text-sidebar-accent-foreground font-semibold shadow-sm'
                        : 'text-muted-foreground hover:bg-sidebar-accent/55 hover:text-foreground',
                      collapsed && 'justify-center px-2',
                    )}
                    title={collapsed ? label : undefined}
                  >
                    {active && <span className="absolute inset-y-2 left-0 w-0.5 rounded-full bg-primary" />}
                    <Icon className={cn('size-4 shrink-0', active && 'text-primary')} />
                    {!collapsed && <span className="truncate">{label}</span>}
                  </button>
                )
              })}
            </div>
          </div>)}
        </nav>

        <div className="border-t border-sidebar-border/80 p-2">
          <div
            className={cn(
              'flex items-center gap-2 rounded-lg px-2 py-2',
              collapsed ? 'justify-center' : 'justify-between',
            )}
          >
            {!collapsed && (
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-semibold">{user?.username ?? '用户'}</p>
                <p className="text-muted-foreground truncate text-[11px]">{user?.email}</p>
              </div>
            )}
            <NotificationBell />
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
          <header className="flex h-16 shrink-0 items-center justify-between border-b bg-card/75 px-6 backdrop-blur-xl">
            <div className="flex items-center gap-2 text-sm">
              <LayoutDashboard className="text-primary size-4" />
              <ChevronRight className="text-muted-foreground size-3" />
              <span className="font-semibold">{breadcrumbLabel(location.pathname)}</span>
            </div>
            <span className="hidden items-center gap-2 text-xs text-muted-foreground sm:flex">
              <span className="size-1.5 rounded-full bg-emerald-500" />平台服务正常
            </span>
          </header>
        )}

        <main className={cn('min-h-0 flex-1 overflow-auto', isChatPage ? 'p-0' : 'p-5 md:p-7')}>
          <Suspense fallback={<div className="flex h-full min-h-48 items-center justify-center text-sm text-muted-foreground">正在加载工作区…</div>}>
            <Routes>
              <Route path="/" element={<AgentListPage />} />
              <Route path="/agents" element={<AgentListPage />} />
              <Route path="/network" element={<AgentNetworkPage />} />
              <Route path="/agents/new/config" element={<AgentConfigPage />} />
              <Route path="/agents/:id/config" element={<AgentConfigPage />} />
              {/* 用可选段而非两条独立 Route：否则带/不带 session_id 之间跳转会
                  被当成不同路由，导致 SessionPage 被卸载重装，正在流式写入的
                  聊天状态会被清空 */}
              <Route path="/agents/:agentId/chat/:sessionId?" element={<SessionPage />} />
              <Route path="/memories" element={<MemoriesPage />} />
              <Route path="/knowledge" element={<KnowledgePage />} />
              <Route path="/schedules" element={<SchedulesPage />} />
              <Route path="/account" element={<AccountPage />} />
              <Route path="/users" element={<UsersPage />} />
              <Route path="/tools" element={<ToolsPage />} />
              <Route path="/skills" element={<SkillsPage />} />
              <Route path="/models" element={<ModelsPage />} />
              <Route path="/governance" element={<GovernancePage />} />
              <Route path="/audit" element={<AuditPage />} />
              <Route path="/runtime" element={<RuntimePage />} />
              <Route path="/usage" element={<UsagePage />} />
              <Route path="/admin" element={<Navigate to="/users" replace />} />
              <Route path="/sessions/:sessionId/trace" element={<TracePage />} />
            </Routes>
          </Suspense>
        </main>
      </div>
    </div>
  )
}
