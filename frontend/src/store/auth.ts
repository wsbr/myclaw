import { create } from 'zustand'

interface User {
  id: string
  user_id: string
  email: string | null
  username: string
  role: string
  roles: string[]
  status: string
  version: number
}

interface AuthState {
  user: User | null
  token: string | null
  setAuth: (user: User, token: string) => void
  updateUser: (values: Partial<User>) => void
  logout: () => void
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  token: localStorage.getItem('access_token'),
  setAuth: (user, token) => {
    localStorage.setItem('access_token', token)
    set({ user, token })
  },
  updateUser: (values) => set((state) => ({
    user: state.user ? { ...state.user, ...values } : null,
  })),
  logout: () => {
    localStorage.removeItem('access_token')
    set({ user: null, token: null })
  },
}))
