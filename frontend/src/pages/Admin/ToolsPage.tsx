import { AdminSectionPage } from './AdminSectionPage'
import ToolsTab from './ToolsTab'

export default function ToolsPage() {
  return (
    <AdminSectionPage title="Tool 管理" description="治理代码注册的 Tool、确认策略与平台配置">
      <ToolsTab />
    </AdminSectionPage>
  )
}
