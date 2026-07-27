import { AdminSectionPage } from './AdminSectionPage'
import SkillsTab from './SkillsTab'

export default function SkillsPage() {
  return (
    <AdminSectionPage title="Skill 管理" description="管理平台 Skill 提示词片段">
      <SkillsTab />
    </AdminSectionPage>
  )
}
