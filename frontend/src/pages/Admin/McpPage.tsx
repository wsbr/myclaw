import { AdminSectionPage } from './AdminSectionPage'
import McpTab from './McpTab'

export default function McpPage() {
  return (
    <AdminSectionPage title="MCP Server" description="管理外部 MCP 服务连接">
      <McpTab />
    </AdminSectionPage>
  )
}
