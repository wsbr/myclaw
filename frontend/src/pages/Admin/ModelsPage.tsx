import { AdminSectionPage } from './AdminSectionPage'
import ModelsTab from './ModelsTab'

export default function ModelsPage() {
  return (
    <AdminSectionPage title="模型 Provider" description="管理模型供应商与可用模型">
      <ModelsTab />
    </AdminSectionPage>
  )
}
