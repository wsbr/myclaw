import type { ReactNode } from 'react'
import { Card } from 'antd'

interface AdminSectionPageProps {
  title: string
  description: string
  children: ReactNode
}

export function AdminSectionPage({ title, description, children }: AdminSectionPageProps) {
  return (
    <div className="mx-auto max-w-6xl">
      <div className="mb-6">
        <h2 className="text-2xl font-semibold tracking-tight">{title}</h2>
        <p className="text-muted-foreground mt-1 text-sm">{description}</p>
      </div>
      <Card className="rounded-xl shadow-sm" bordered={false}>
        {children}
      </Card>
    </div>
  )
}
