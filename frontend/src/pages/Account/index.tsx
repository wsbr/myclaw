import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Descriptions, Form, Input, Modal, Space, Spin, Tag, Typography, message } from 'antd'
import { accountsApi, MyAccount } from '@/api/accounts'
import { useAuthStore } from '@/store/auth'

type ProfileValues = { display_name: string; email?: string }

export default function AccountPage() {
  const [form] = Form.useForm<ProfileValues>()
  const [assertion, setAssertion] = useState('')
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { updateUser, logout } = useAuthStore()
  const { data: account, isLoading } = useQuery({
    queryKey: ['account', 'me'], queryFn: accountsApi.me,
  })

  const refreshAccount = (updated: MyAccount) => {
    queryClient.setQueryData(['account', 'me'], updated)
    updateUser({
      tenant_id: updated.tenant_id,
      user_id: updated.user_id,
      email: updated.email,
      username: updated.display_name,
      roles: updated.roles,
      role: updated.roles.includes('admin') ? 'admin' : updated.roles.includes('builder') ? 'builder' : 'user',
      status: updated.status,
      version: updated.version,
    })
  }

  const updateProfile = useMutation({
    mutationFn: async (values: ProfileValues) => {
      if (!account) throw new Error('account_not_loaded')
      const changes: { display_name?: string; email?: string | null } = {}
      if (values.display_name.trim() !== account.display_name) changes.display_name = values.display_name.trim()
      const email = values.email?.trim() || null
      if (email !== account.email) changes.email = email
      if (!Object.keys(changes).length) return { updated: account, emailChanged: false }
      return { updated: await accountsApi.update(changes, account.version), emailChanged: 'email' in changes }
    },
    onSuccess: ({ updated, emailChanged }) => {
      refreshAccount(updated)
      if (emailChanged) {
        message.success('邮箱已更新，请重新登录')
        logout()
        navigate('/login', { replace: true })
      } else {
        message.success('账号资料已更新')
      }
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || '账号资料更新失败'),
  })

  const changeUserId = useMutation({
    mutationFn: () => accountsApi.changeUserId(assertion.trim(), account!.version),
    onSuccess: (updated) => {
      refreshAccount(updated)
      message.success('userId 已更新，请以新身份重新登录')
      logout()
      navigate('/login', { replace: true })
    },
    onError: (error: any) => message.error(error?.response?.data?.error?.code || 'userId 更新失败'),
  })

  if (isLoading || !account) return <div className="py-24 text-center"><Spin /></div>

  return <div className="mx-auto max-w-3xl space-y-4">
    <div>
      <Typography.Title level={2} style={{ margin: 0 }}>我的账号</Typography.Title>
      <Typography.Text type="secondary">管理登录资料与企业 canonical userId</Typography.Text>
    </div>
    <Card title="账号资料">
      <Descriptions column={1} size="small" className="mb-5">
        <Descriptions.Item label="当前 Tenant">
          <Space>
            <Tag color="blue">{account.tenant?.name ?? 'Unknown Tenant'}</Tag>
            <Typography.Text copyable code>{account.tenant_id}</Typography.Text>
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="当前 userId"><Typography.Text copyable code>{account.user_id}</Typography.Text></Descriptions.Item>
        <Descriptions.Item label="角色"><Space>{account.roles.map(role => <Tag key={role}>{role}</Tag>)}</Space></Descriptions.Item>
        <Descriptions.Item label="状态"><Tag color="green">{account.status}</Tag></Descriptions.Item>
      </Descriptions>
      <Form form={form} layout="vertical" initialValues={{
        display_name: account.display_name, email: account.email ?? undefined,
      }} onFinish={values => updateProfile.mutate(values)}>
        <Form.Item name="display_name" label="显示名称" rules={[{ required: true }, { max: 200 }]}>
          <Input autoComplete="name" />
        </Form.Item>
        <Form.Item name="email" label="登录邮箱" rules={[{ type: 'email' }, { max: 255 }]}>
          <Input autoComplete="email" />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={updateProfile.isPending}>保存资料</Button>
      </Form>
    </Card>
    <Card title="更新企业 userId">
      <Alert type="warning" showIcon className="mb-4"
        message="userId 变更会被视为新人"
        description="旧 Session、Memory、Plan、Group、Artifact 与执行历史不会迁移或合并；新 userId 只能访问新身份下的数据。" />
      <Input.TextArea value={assertion} onChange={event => setAssertion(event.target.value)}
        placeholder="粘贴 aud=myclaw-user-id-link 的短期企业身份断言" autoSize={{ minRows: 3, maxRows: 8 }} />
      <Button danger className="mt-3" disabled={assertion.trim().length < 20} loading={changeUserId.isPending}
        onClick={() => Modal.confirm({
          title: '确认切换 canonical userId？',
          content: '这是身份切换，不会迁移任何旧数据。成功后当前登录会立即失效。',
          okText: '确认切换', okButtonProps: { danger: true },
          onOk: () => changeUserId.mutateAsync(),
        })}>验证并切换 userId</Button>
    </Card>
  </div>
}
