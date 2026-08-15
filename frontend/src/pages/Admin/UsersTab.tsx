import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Form, Input, message, Modal, Select, Space, Table, Tag } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { useState } from 'react'

import { adminApi, type PlatformAccount } from '@/api/admin'
import { displayLabel } from '@/lib/presentation'


const roleOptions = [
  { value: 'user', label: '普通用户' },
  { value: 'builder', label: '构建者' },
  { value: 'admin', label: '管理员' },
]


export default function UsersTab() {
  const queryClient = useQueryClient()
  const [createOpen, setCreateOpen] = useState(false)
  const [editAccount, setEditAccount] = useState<PlatformAccount | null>(null)
  const [createForm] = Form.useForm()
  const [editForm] = Form.useForm()
  const { data, isLoading } = useQuery({
    queryKey: ['admin', 'accounts'],
    queryFn: adminApi.listAccounts,
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['admin', 'accounts'] })
  const createMutation = useMutation({
    mutationFn: adminApi.createAccount,
    onSuccess: async () => {
      message.success('平台账号已创建')
      await refresh()
      setCreateOpen(false)
      createForm.resetFields()
    },
    onError: () => message.error('创建失败，请检查 userId、邮箱或权限'),
  })
  const rolesMutation = useMutation({
    mutationFn: ({ account, roles }: { account: PlatformAccount; roles: string[] }) => adminApi.updateRoles(account, roles),
  })
  const statusMutation = useMutation({
    mutationFn: ({ account, status }: { account: PlatformAccount; status: 'active' | 'disabled' }) => adminApi.updateStatus(account, status),
  })

  const submitEdit = async (values: { roles: string[]; status: 'active' | 'disabled' }) => {
    if (!editAccount) return
    try {
      let current = editAccount
      if ([...values.roles].sort().join(',') !== [...current.roles].sort().join(',')) {
        current = await rolesMutation.mutateAsync({ account: current, roles: values.roles })
      }
      if (values.status !== current.status) {
        current = await statusMutation.mutateAsync({ account: current, status: values.status })
      }
      message.success('账号已更新；角色或状态变化会使旧登录凭证失效')
      await refresh()
      setEditAccount(null)
    } catch {
      message.error('更新失败，可能存在版本冲突，请刷新后重试')
      await refresh()
    }
  }

  const columns = [
    {
      title: '租户', dataIndex: 'tenant', key: 'tenant',
      render: (_: unknown, account: PlatformAccount) => (
        <Tag color="blue">{account.tenant?.name ?? account.tenant_id}</Tag>
      ),
    },
    { title: '用户标识', dataIndex: 'user_id', key: 'user_id' },
    { title: '邮箱', dataIndex: 'email', key: 'email', render: (value: string | null) => value || '—' },
    { title: '显示名称', dataIndex: 'display_name', key: 'display_name' },
    {
      title: '角色', dataIndex: 'roles', key: 'roles',
      render: (roles: string[]) => <Space size={4}>{roles.map(role => <Tag key={role} color={role === 'admin' ? 'red' : role === 'builder' ? 'gold' : 'blue'}>{displayLabel(role)}</Tag>)}</Space>,
    },
    {
      title: '状态', dataIndex: 'status', key: 'status',
      render: (status: string) => <Tag color={status === 'active' ? 'success' : 'default'}>{displayLabel(status)}</Tag>,
    },
    { title: '版本', dataIndex: 'version', key: 'version', width: 80 },
    {
      title: '操作', key: 'actions',
      render: (_: unknown, account: PlatformAccount) => (
        <Button size="small" onClick={() => {
          setEditAccount(account)
          editForm.setFieldsValue({ roles: account.roles, status: account.status })
        }}>角色 / 状态</Button>
      ),
    },
  ]

  return (
    <>
      <div className="mb-3 flex justify-end">
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新建账号</Button>
      </div>
      <Table dataSource={data?.items ?? []} columns={columns} rowKey="id" loading={isLoading} />

      <Modal title="新建平台账号" open={createOpen} onCancel={() => setCreateOpen(false)}
        onOk={() => createForm.submit()} confirmLoading={createMutation.isPending}>
        <Form form={createForm} onFinish={createMutation.mutate} layout="vertical"
          initialValues={{ roles: ['user'] }}>
          <Form.Item name="user_id" label="企业统一用户标识（留空自动生成）"><Input /></Form.Item>
          <Form.Item name="email" label="邮箱" rules={[{ required: true, type: 'email' }]}><Input /></Form.Item>
          <Form.Item name="display_name" label="显示名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="password" label="初始密码" rules={[{ required: true, min: 8 }]}><Input.Password /></Form.Item>
          <Form.Item name="roles" label="平台角色" rules={[{ required: true }]}>
            <Select mode="multiple" options={roleOptions} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal title={`编辑账号：${editAccount?.display_name ?? ''}`} open={!!editAccount}
        onCancel={() => setEditAccount(null)} onOk={() => editForm.submit()}
        confirmLoading={rolesMutation.isPending || statusMutation.isPending}>
        <Form form={editForm} onFinish={submitEdit} layout="vertical">
          <Form.Item name="roles" label="平台角色" rules={[{ required: true }]}>
            <Select mode="multiple" options={roleOptions} />
          </Form.Item>
          <Form.Item name="status" label="账号状态" rules={[{ required: true }]}>
            <Select options={[{ value: 'active', label: '启用' }, { value: 'disabled', label: '禁用' }]} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
