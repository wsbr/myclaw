import { Form, Input, message } from 'antd'
import { useNavigate } from 'react-router-dom'
import { Bot, Boxes, Network, ShieldCheck } from 'lucide-react'
import { authApi } from '@/api/auth'
import { API_BASE } from '@/api/client'
import { useAuthStore } from '@/store/auth'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'

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

  const highlights = [
    { label: '智能体装配', icon: Boxes },
    { label: '协作编排', icon: Network },
    { label: '安全治理', icon: ShieldCheck },
  ]

  return (
    <div className="subtle-grid relative flex min-h-screen items-center justify-center overflow-hidden bg-background p-6">
      <div className="pointer-events-none absolute -left-28 -top-24 size-96 rounded-full bg-primary/10 blur-3xl" />
      <div className="pointer-events-none absolute -bottom-32 -right-24 size-96 rounded-full bg-amber-200/25 blur-3xl" />
      <div className="animate-fade-in relative grid w-full max-w-5xl overflow-hidden rounded-[28px] border bg-card/90 shadow-[0_30px_90px_-45px_rgba(28,37,84,0.45)] backdrop-blur-xl lg:grid-cols-[1.05fr_0.95fr]">
        <section className="hidden flex-col justify-between bg-primary p-10 text-primary-foreground lg:flex">
          <div>
            <div className="mb-14 flex items-center gap-3">
              <span className="flex size-10 items-center justify-center rounded-2xl bg-white/12"><Bot className="size-5" /></span>
              <span className="font-serif text-2xl font-semibold">MyClaw</span>
            </div>
            <h1 className="max-w-md text-3xl font-semibold leading-tight tracking-tight">让每个智能体各司其职，协同完成复杂工作</h1>
            <p className="mt-4 max-w-md text-sm leading-7 text-white/70">从智能体装配、运行编排到群组协作与全链路治理，统一在一个企业级工作台中完成。</p>
          </div>
          <div className="grid grid-cols-3 gap-3 text-xs">
            {highlights.map(({ label, icon: Icon }) => <div key={label} className="rounded-2xl border border-white/10 bg-white/5 p-3">
              <Icon className="mb-2 size-4 text-white/80" />
              <span className="text-white/75">{label}</span>
            </div>)}
          </div>
        </section>

        <section className="p-7 sm:p-10 lg:p-12">
          <div className="mb-8 lg:hidden">
            <div className="mb-4 flex size-12 items-center justify-center rounded-2xl bg-primary text-primary-foreground"><Bot className="size-6" /></div>
            <h1 className="font-serif text-3xl font-semibold text-primary">MyClaw</h1>
            <p className="mt-2 text-sm text-muted-foreground">企业级智能体装配与编排平台</p>
          </div>
          <div className="mb-7">
            <p className="text-xs font-semibold tracking-[0.16em] text-primary">欢迎回来</p>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight">登录工作台</h2>
            <p className="mt-2 text-sm text-muted-foreground">使用平台账号继续管理和运行智能体</p>
          </div>

          <Card className="border-0 bg-transparent shadow-none">
            <CardContent className="p-0">
              <Form form={form} onFinish={onFinish} layout="vertical" requiredMark={false}>
                <Form.Item
                  name="email"
                  label={<span className="text-sm font-medium">邮箱</span>}
                  rules={[{ required: true, type: 'email', message: '请输入有效邮箱' }]}
                >
                  <Input size="large" placeholder="请输入登录邮箱" className="rounded-xl" />
                </Form.Item>
                <Form.Item
                  name="password"
                  label={<span className="text-sm font-medium">密码</span>}
                  rules={[{ required: true, message: '请输入密码' }]}
                >
                  <Input.Password size="large" placeholder="请输入密码" className="rounded-xl" />
                </Form.Item>
                <Form.Item className="mb-0 mt-2">
                  <Button type="submit" className="h-11 w-full text-sm">登录</Button>
                </Form.Item>
              </Form>
              <div className="my-5 flex items-center gap-3 text-xs text-muted-foreground">
                <span className="h-px flex-1 bg-border" />或<span className="h-px flex-1 bg-border" />
              </div>
              <Button
                type="button"
                variant="outline"
                className="h-11 w-full text-sm"
                onClick={() => window.location.assign(`${API_BASE}/auth/enterprise/start`)}
              >
                使用企业身份登录
              </Button>
            </CardContent>
          </Card>
        </section>
      </div>
    </div>
  )
}
