import { AdminSectionPage } from './AdminSectionPage'
import ModelsTab from './ModelsTab'

export default function ModelsPage() {
  return (
    <AdminSectionPage title="模型注册表" description="管理可执行模型、凭证引用、能力与默认参数">
      <ModelsTab />
    </AdminSectionPage>
  )
}
