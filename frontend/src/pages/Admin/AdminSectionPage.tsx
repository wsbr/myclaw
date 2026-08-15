import type { ReactNode } from 'react'
import { Card } from 'antd'

interface AdminSectionPageProps {
  title: string
  description: string
  children: ReactNode
}

export function AdminSectionPage({ title, description, children }: AdminSectionPageProps) {
  return (
    <div className="app-page">
      <div className="page-header">
        <div>
          <h2 className="page-title">{title}</h2>
          <p className="page-description">{description}</p>
        </div>
      </div>
      <Card className="surface-panel" bordered={false}>
        {children}
      </Card>
    </div>
  )
}
