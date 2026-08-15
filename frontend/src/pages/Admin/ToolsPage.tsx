import { AdminSectionPage } from './AdminSectionPage'
import ToolsTab from './ToolsTab'

export default function ToolsPage() {
  return (
    <AdminSectionPage title="工具管理" description="治理平台工具、外部连接、确认策略与执行边界">
      <ToolsTab />
    </AdminSectionPage>
  )
}
