import { AdminSectionPage } from './AdminSectionPage'
import ModelsTab from './ModelsTab'

export default function ModelsPage() {
  return (
    <AdminSectionPage title="模型注册表" description="管理模型 API 地址、完整 API Key、能力与默认参数">
      <ModelsTab />
    </AdminSectionPage>
  )
}
