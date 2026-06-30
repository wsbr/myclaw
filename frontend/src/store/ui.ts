import { create } from 'zustand'

interface UIState {
  siderCollapsed: boolean
  toggleSider: () => void
  setSiderCollapsed: (v: boolean) => void
}

export const useUIStore = create<UIState>((set) => ({
  siderCollapsed: false,
  toggleSider: () => set(s => ({ siderCollapsed: !s.siderCollapsed })),
  setSiderCollapsed: (v) => set({ siderCollapsed: v }),
}))
