import { Layout, Menu, Typography, Avatar, Dropdown } from 'antd'
import {
  RobotOutlined, ThunderboltOutlined, SettingOutlined, LogoutOutlined, UserOutlined,
  BranchesOutlined,
} from '@ant-design/icons'
import { Routes, Route, useNavigate, useLocation } from 'react-router-dom'
import { useAuthStore } from '../../store/auth'
import AgentListPage from '../Agents/List'
import AgentConfigPage from '../Agents/Config'
import SessionPage from '../Session'
import TasksPage from '../Tasks'
import TracePage from '../Trace'
import OrchestrationPage from '../Orchestration'
import AdminPage from '../Admin'

const { Sider, Header, Content } = Layout

const menuItems = [
  { key: '/agents', icon: <RobotOutlined />, label: 'Agents' },
  { key: '/tasks', icon: <ThunderboltOutlined />, label: 'Tasks' },
  { key: '/admin', icon: <SettingOutlined />, label: 'Admin' },
]

export default function DashboardPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const { user, logout } = useAuthStore()

  const selectedKey = menuItems.find(m => location.pathname.startsWith(m.key))?.key ?? '/agents'

  const userMenu = {
    items: [{ key: 'logout', icon: <LogoutOutlined />, label: '退出登录' }],
    onClick: ({ key }: { key: string }) => {
      if (key === 'logout') { logout(); navigate('/login') }
    },
  }

  return (
    <Layout style={{ height: '100vh' }}>
      <Sider theme="dark" width={200}>
        <div style={{ padding: '16px', color: '#fff', fontWeight: 700, fontSize: 18 }}>
          🦀 MyClaw
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedKey]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <Layout>
        <Header style={{ background: '#fff', padding: '0 24px', display: 'flex', justifyContent: 'flex-end', alignItems: 'center' }}>
          <Dropdown menu={userMenu}>
            <div style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8 }}>
              <Avatar icon={<UserOutlined />} />
              <Typography.Text>{user?.username ?? 'User'}</Typography.Text>
            </div>
          </Dropdown>
        </Header>
        <Content style={{ padding: 24, overflow: 'auto', height: 'calc(100vh - 64px)' }}>
          <Routes>
            <Route path="/" element={<AgentListPage />} />
            <Route path="/agents" element={<AgentListPage />} />
            <Route path="/agents/new/config" element={<AgentConfigPage />} />
            <Route path="/agents/:id/config" element={<AgentConfigPage />} />
            <Route path="/agents/:agentId/chat" element={<SessionPage />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/sessions/:sessionId/trace" element={<TracePage />} />
            <Route path="/sessions/:sessionId/orchestration" element={<OrchestrationPage />} />
            <Route path="/admin" element={<AdminPage />} />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  )
}
