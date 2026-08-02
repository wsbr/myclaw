import { AdminSectionPage } from './AdminSectionPage'
import SkillsTab from './SkillsTab'

export default function SkillsPage() {
  return (
    <AdminSectionPage title="Skill 管理" description="上传、发布并同步可移植 SKILL.md 能力包">
      <SkillsTab />
    </AdminSectionPage>
  )
}
