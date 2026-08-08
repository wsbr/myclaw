import { lazy, Suspense, useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { useAuthStore } from './store/auth'
import { authApi } from './api/auth'

const LoginPage = lazy(() => import('./pages/Login'))
const EnterpriseCallbackPage = lazy(() => import('./pages/Login/EnterpriseCallback'))
const WorkspaceLayout = lazy(() => import('./components/layout/WorkspaceLayout'))

function PrivateRoute({ children }: { children: React.ReactNode }) {
  const { token, user, setAuth, logout } = useAuthStore()
  const [checking, setChecking] = useState(Boolean(token && !user))

  useEffect(() => {
    if (!token || user) {
      setChecking(false)
      return
    }
    let active = true
    authApi.me()
      .then((current) => { if (active) setAuth(current, token) })
      .catch(() => { if (active) logout() })
      .finally(() => { if (active) setChecking(false) })
    return () => { active = false }
  }, [token, user, setAuth, logout])

  if (!token) return <Navigate to="/login" replace />
  if (checking || !user) {
    return <div className="flex h-screen items-center justify-center text-sm text-muted-foreground">正在验证登录状态…</div>
  }
  return <>{children}</>
}

export default function App() {
  return (
    <BrowserRouter>
      <Suspense fallback={<div className="flex h-screen items-center justify-center text-sm text-muted-foreground">正在加载 MyClaw…</div>}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/enterprise-callback" element={<EnterpriseCallbackPage />} />
          <Route
            path="/*"
            element={
              <PrivateRoute>
                <WorkspaceLayout />
              </PrivateRoute>
            }
          />
        </Routes>
      </Suspense>
    </BrowserRouter>
  )
}
