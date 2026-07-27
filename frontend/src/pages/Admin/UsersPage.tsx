import { AdminSectionPage } from './AdminSectionPage'
import UsersTab from './UsersTab'

export default function UsersPage() {
  return (
    <AdminSectionPage title="用户管理" description="管理平台用户账号与角色">
      <UsersTab />
    </AdminSectionPage>
  )
}
