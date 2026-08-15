import { AdminSectionPage } from './AdminSectionPage'
import ModelsTab from './ModelsTab'

export default function ModelsPage() {
  return (
    <AdminSectionPage title="模型注册表" description="管理模型接口地址、访问密钥、能力与默认参数">
      <ModelsTab />
    </AdminSectionPage>
  )
}
