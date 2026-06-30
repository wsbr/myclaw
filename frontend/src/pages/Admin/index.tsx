import { Tabs } from 'antd'
import UsersTab from './UsersTab'
import ToolsTab from './ToolsTab'
import SkillsTab from './SkillsTab'
import McpTab from './McpTab'
import ModelsTab from './ModelsTab'

export default function AdminPage() {
  return (
    <Tabs
      defaultActiveKey="users"
      items={[
        { key: 'users', label: '用户管理', children: <UsersTab /> },
        { key: 'tools', label: 'Tool 管理', children: <ToolsTab /> },
        { key: 'skills', label: 'Skill 管理', children: <SkillsTab /> },
        { key: 'mcp', label: 'MCP Server', children: <McpTab /> },
        { key: 'models', label: '模型 Provider', children: <ModelsTab /> },
      ]}
    />
  )
}
