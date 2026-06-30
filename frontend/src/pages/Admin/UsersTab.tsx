import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Table, Button, Form, Input, Select, Modal, Tag, message, Space, Popconfirm } from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons'
import { useState } from 'react'
import { adminApi } from '../../api/admin'

export default function UsersTab() {
  const qc = useQueryClient()
  const [createOpen, setCreateOpen] = useState(false)
  const [editUser, setEditUser] = useState<any>(null)
  const [createForm] = Form.useForm()
  const [editForm] = Form.useForm()

  const { data: users, isLoading } = useQuery({
    queryKey: ['admin', 'users'],
    queryFn: adminApi.listUsers,
  })

  const createMutation = useMutation({
    mutationFn: adminApi.createUser,
    onSuccess: () => {
      message.success('用户已创建')
      qc.invalidateQueries({ queryKey: ['admin', 'users'] })
      setCreateOpen(false)
      createForm.resetFields()
    },
    onError: () => message.error('创建失败'),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: any }) => adminApi.updateUser(id, data),
    onSuccess: () => {
      message.success('已更新')
      qc.invalidateQueries({ queryKey: ['admin', 'users'] })
      setEditUser(null)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: adminApi.deleteUser,
    onSuccess: () => { message.success('已删除'); qc.invalidateQueries({ queryKey: ['admin', 'users'] }) },
  })

  const columns = [
    { title: '邮箱', dataIndex: 'email', key: 'email' },
    { title: '用户名', dataIndex: 'username', key: 'username' },
    { title: '角色', dataIndex: 'role', key: 'role', render: (v: string) => <Tag color={v === 'admin' ? 'red' : 'blue'}>{v}</Tag> },
    { title: '状态', dataIndex: 'is_active', key: 'is_active', render: (v: boolean) => <Tag color={v ? 'success' : 'default'}>{v ? '正常' : '禁用'}</Tag> },
    {
      title: '操作', key: 'actions',
      render: (_: any, r: any) => (
        <Space>
          <Button
            size="small"
            icon={<EditOutlined />}
            onClick={() => { setEditUser(r); editForm.setFieldsValue({ username: r.username, role: r.role, is_active: r.is_active }) }}
          >编辑</Button>
          <Popconfirm title="确认删除此用户？" onConfirm={() => deleteMutation.mutate(r.id)}>
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新建用户</Button>
      </div>
      <Table dataSource={users || []} columns={columns} rowKey="id" loading={isLoading} />

      <Modal title="新建用户" open={createOpen} onCancel={() => setCreateOpen(false)} onOk={() => createForm.submit()} confirmLoading={createMutation.isPending}>
        <Form form={createForm} onFinish={createMutation.mutate} layout="vertical">
          <Form.Item name="email" label="邮箱" rules={[{ required: true, type: 'email' }]}><Input /></Form.Item>
          <Form.Item name="username" label="用户名" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true }]}><Input.Password /></Form.Item>
          <Form.Item name="role" label="角色" initialValue="user">
            <Select options={[{ value: 'user', label: '普通用户' }, { value: 'admin', label: '管理员' }]} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="编辑用户"
        open={!!editUser}
        onCancel={() => setEditUser(null)}
        onOk={() => editForm.submit()}
        confirmLoading={updateMutation.isPending}
      >
        <Form
          form={editForm}
          onFinish={(v) => updateMutation.mutate({ id: editUser.id, data: v })}
          layout="vertical"
        >
          <Form.Item name="username" label="用户名"><Input /></Form.Item>
          <Form.Item name="role" label="角色">
            <Select options={[{ value: 'user', label: '普通用户' }, { value: 'admin', label: '管理员' }]} />
          </Form.Item>
          <Form.Item name="is_active" label="状态">
            <Select options={[{ value: true, label: '正常' }, { value: false, label: '禁用' }]} />
          </Form.Item>
          <Form.Item name="password" label="重置密码（留空不修改）">
            <Input.Password />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
