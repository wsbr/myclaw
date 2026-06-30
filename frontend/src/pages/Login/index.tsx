import { Form, Input, Button, Card, Typography, message } from 'antd'
import { useNavigate } from 'react-router-dom'
import { authApi } from '../../api/auth'
import { useAuthStore } from '../../store/auth'

export default function LoginPage() {
  const navigate = useNavigate()
  const setAuth = useAuthStore((s) => s.setAuth)
  const [form] = Form.useForm()

  const onFinish = async (values: { email: string; password: string }) => {
    try {
      const data = await authApi.login(values.email, values.password)
      setAuth(data.user, data.access_token)
      navigate('/')
    } catch {
      message.error('邮箱或密码错误')
    }
  }

  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh', background: '#f0f2f5' }}>
      <Card style={{ width: 400 }}>
        <Typography.Title level={3} style={{ textAlign: 'center', marginBottom: 32 }}>
          MyClaw
        </Typography.Title>
        <Form form={form} onFinish={onFinish} layout="vertical">
          <Form.Item name="email" label="邮箱" rules={[{ required: true, type: 'email' }]}>
            <Input size="large" placeholder="admin@myclaw.local" />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true }]}>
            <Input.Password size="large" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" size="large" block>
              登录
            </Button>
          </Form.Item>
        </Form>
      </Card>
    </div>
  )
}
