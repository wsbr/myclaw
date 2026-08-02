import { useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Alert, Spin } from 'antd'
import { authApi } from '@/api/auth'
import { useAuthStore } from '@/store/auth'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

export default function EnterpriseCallbackPage() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const setAuth = useAuthStore((state) => state.setAuth)
  const started = useRef(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (started.current) return
    started.current = true
    const assertion = params.get('assertion')
    if (!assertion) {
      setError('企业身份回调缺少 assertion。')
      return
    }
    authApi.enterpriseCallback(assertion)
      .then((data) => {
        setAuth(data.user, data.access_token)
        navigate('/', { replace: true })
      })
      .catch((reason) => {
        setError(reason?.response?.data?.error?.message || '企业身份验证失败。')
      })
  }, [navigate, params, setAuth])

  return (
    <div className="bg-background flex min-h-screen items-center justify-center p-6">
      <Card className="w-full max-w-md shadow-md">
        <CardHeader><CardTitle>企业身份登录</CardTitle></CardHeader>
        <CardContent>
          {error
            ? <Alert type="error" showIcon message={error} />
            : <div className="flex items-center gap-3 text-sm text-muted-foreground"><Spin size="small" />正在验证企业身份…</div>}
        </CardContent>
      </Card>
    </div>
  )
}
