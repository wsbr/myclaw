import { useQuery } from '@tanstack/react-query'
import { Table, Tag } from 'antd'
import client from '../../api/client'

export default function ToolsTab() {
  const { data, isLoading } = useQuery({
    queryKey: ['tools'],
    queryFn: () => client.get('/tools').then(r => r.data),
  })
  const columns = [
    { title: '名称', dataIndex: 'name', key: 'name' },
    { title: '显示名', dataIndex: 'display_name', key: 'display_name' },
    { title: '描述', dataIndex: 'description', key: 'description', ellipsis: true },
    { title: '内置', dataIndex: 'is_builtin', key: 'is_builtin', render: (v: boolean) => <Tag color={v ? 'blue' : 'default'}>{v ? '内置' : '自定义'}</Tag> },
    { title: '版本', dataIndex: 'version', key: 'version' },
  ]
  return <Table dataSource={data || []} columns={columns} rowKey="id" loading={isLoading} />
}
