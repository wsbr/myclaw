import SkillsTab from './SkillsTab'

export default function SkillsPage() {
  return (
    <div className="app-page max-w-[1500px]">
      <div className="page-header">
        <div>
        <h2 className="page-title">技能管理</h2>
        <p className="page-description">
          浏览、在线编辑、发布并同步可移植技能；每次修改都会生成新的不可变版本。
        </p>
        </div>
      </div>
      <SkillsTab />
    </div>
  )
}
