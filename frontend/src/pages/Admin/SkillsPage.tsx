import SkillsTab from './SkillsTab'

export default function SkillsPage() {
  return (
    <div className="mx-auto max-w-[1500px]">
      <div className="mb-6">
        <h2 className="text-2xl font-semibold tracking-tight">Skill 管理</h2>
        <p className="text-muted-foreground mt-1 text-sm">
          浏览、在线编辑、发布并同步可移植 Skill；每次修改都会生成新的不可变版本。
        </p>
      </div>
      <SkillsTab />
    </div>
  )
}
