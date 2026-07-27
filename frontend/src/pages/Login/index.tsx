import { Form, Input, message } from 'antd'
import { useNavigate } from 'react-router-dom'
import { Bot } from 'lucide-react'
import { authApi } from '@/api/auth'
import { useAuthStore } from '@/store/auth'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

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
    <div className="bg-background flex min-h-screen items-center justify-center p-6">
      <div className="animate-fade-in w-full max-w-md">
        <div className="mb-8 text-center">
          <div className="bg-primary/10 text-primary mx-auto mb-4 flex size-14 items-center justify-center rounded-2xl">
            <Bot className="size-7" />
          </div>
          <h1 className="text-primary font-serif text-3xl font-semibold">MyClaw</h1>
          <p className="text-muted-foreground mt-2 text-sm">企业级 Agent 装配与编排平台</p>
        </div>

        <Card className="shadow-md">
          <CardHeader>
            <CardTitle>登录</CardTitle>
            <CardDescription>使用管理员或用户账号登录工作台</CardDescription>
          </CardHeader>
          <CardContent>
            <Form form={form} onFinish={onFinish} layout="vertical" requiredMark={false}>
              <Form.Item
                name="email"
                label={<span className="text-sm font-medium">邮箱</span>}
                rules={[{ required: true, type: 'email', message: '请输入有效邮箱' }]}
              >
                <Input size="large" placeholder="admin@myclaw.local" className="rounded-lg" />
              </Form.Item>
              <Form.Item
                name="password"
                label={<span className="text-sm font-medium">密码</span>}
                rules={[{ required: true, message: '请输入密码' }]}
              >
                <Input.Password size="large" className="rounded-lg" />
              </Form.Item>
              <Form.Item className="mb-0 mt-2">
                <Button type="submit" className="h-10 w-full text-sm">
                  登录
                </Button>
              </Form.Item>
            </Form>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
