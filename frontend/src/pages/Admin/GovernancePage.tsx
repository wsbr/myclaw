import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Form, Input, Modal, Space, Switch, Table, Tabs, Tag, message } from 'antd'
import { EditOutlined } from '@ant-design/icons'
import { useState } from 'react'
import client from '../../api/client'
import { AdminSectionPage } from './AdminSectionPage'

type Policy = { id: string; policy_key: string; policy_type: string; name: string; description: string; default_parameters: Record<string, unknown>; parameter_version: number; enabled: boolean }
type Sandbox = { id: string; profile_key: string; type: string; description: string; config: Record<string, unknown>; config_version: number; enabled: boolean }

function GovernanceTable({ kind }: { kind: 'policy' | 'sandbox' }) {
  const queryClient = useQueryClient()
  const [form] = Form.useForm()
  const [editing, setEditing] = useState<Policy | Sandbox | null>(null)
  const endpoint = kind === 'policy' ? '/policy-definitions' : '/sandbox-profiles'
  const queryKey = kind === 'policy' ? 'policy-definitions' : 'sandbox-profiles'
  const rows = useQuery({ queryKey: [queryKey], queryFn: () => client.get(endpoint).then(r => r.data.items) })
  const update = useMutation({
    mutationFn: (values: any) => {
      const version = kind === 'policy' ? (editing as Policy).parameter_version : (editing as Sandbox).config_version
      const jsonField = kind === 'policy' ? 'default_parameters' : 'config'
      return client.patch(`${endpoint}/${editing!.id}`, {
        ...values, [jsonField]: JSON.parse(values[jsonField] || '{}'),
      }, { headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': `"${version}"` } })
    },
    onSuccess: () => { message.success('治理配置已更新'); setEditing(null); queryClient.invalidateQueries({ queryKey: [queryKey] }) },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || '更新失败'),
  })
  const edit = (row: Policy | Sandbox) => {
    setEditing(row)
    form.setFieldsValue({ ...row, [kind === 'policy' ? 'default_parameters' : 'config']: JSON.stringify(kind === 'policy' ? (row as Policy).default_parameters : (row as Sandbox).config, null, 2) })
  }
  return <>
    <Table<any> rowKey="id" loading={rows.isLoading} dataSource={rows.data || []} columns={kind === 'policy' ? [
      { title: 'Key', dataIndex: 'policy_key' }, { title: '类型', dataIndex: 'policy_type', render: value => <Tag>{value}</Tag> },
      { title: '名称', dataIndex: 'name' }, { title: '参数版本', dataIndex: 'parameter_version' },
      { title: '启用', dataIndex: 'enabled', render: value => <Tag color={value ? 'success' : 'default'}>{value ? '启用' : '停用'}</Tag> },
      { title: '操作', render: (_, row: Policy) => <Button size="small" icon={<EditOutlined />} onClick={() => edit(row)}>编辑</Button> },
    ] : [
      { title: 'Key', dataIndex: 'profile_key' }, { title: '类型', dataIndex: 'type', render: value => <Tag>{value}</Tag> },
      { title: '说明', dataIndex: 'description' }, { title: '配置版本', dataIndex: 'config_version' },
      { title: '启用', dataIndex: 'enabled', render: value => <Tag color={value ? 'success' : 'default'}>{value ? '启用' : '停用'}</Tag> },
      { title: '操作', render: (_, row: Sandbox) => <Button size="small" icon={<EditOutlined />} onClick={() => edit(row)}>编辑</Button> },
    ]} />
    <Modal title={kind === 'policy' ? '编辑 Policy' : '编辑 Sandbox Profile'} open={!!editing} onCancel={() => setEditing(null)} onOk={() => form.submit()} confirmLoading={update.isPending}>
      <Form form={form} layout="vertical" onFinish={update.mutate}>
        {kind === 'policy' && <Form.Item name="name" label="名称"><Input /></Form.Item>}
        <Form.Item name="description" label="说明"><Input.TextArea rows={3} /></Form.Item>
        <Form.Item name={kind === 'policy' ? 'default_parameters' : 'config'} label="配置 JSON"><Input.TextArea rows={7} /></Form.Item>
        <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
      </Form>
    </Modal>
  </>
}

export default function GovernancePage() {
  return <AdminSectionPage title="Policy 与 Sandbox" description="治理上下文/记忆策略和会话 Sandbox Profile">
    <Tabs items={[
      { key: 'policies', label: 'Policy Definitions', children: <GovernanceTable kind="policy" /> },
      { key: 'sandboxes', label: 'Sandbox Profiles', children: <GovernanceTable kind="sandbox" /> },
    ]} />
  </AdminSectionPage>
}
