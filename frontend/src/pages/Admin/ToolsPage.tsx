import { AdminSectionPage } from './AdminSectionPage'
import ToolsTab from './ToolsTab'

export default function ToolsPage() {
  return (
    <AdminSectionPage title="Tool 管理" description="查看平台注册的 Tool 列表">
      <ToolsTab />
    </AdminSectionPage>
  )
}
