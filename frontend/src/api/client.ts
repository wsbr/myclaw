import axios from 'axios'

// VITE_API_BASE_URL is baked in at build time (e.g. http://api.internal.com)
// When behind a reverse proxy on the same domain, leave it empty and use relative /api/v1
const API_BASE = (import.meta.env.VITE_API_BASE_URL || '') + '/api/v1'

const client = axios.create({
  baseURL: API_BASE,
  headers: { 'Content-Type': 'application/json' },
})

client.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

client.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem('access_token')
      window.location.href = '/login'
    }
    return Promise.reject(err)
  }
)

export default client
export { API_BASE }

// Derive WebSocket base from API base URL (http→ws, https→wss)
// Falls back to current page host when no VITE_API_BASE_URL (same-domain proxy mode)
export const WS_BASE = import.meta.env.VITE_API_BASE_URL
  ? import.meta.env.VITE_API_BASE_URL.replace(/^http/, 'ws')
  : `ws://${window.location.host}`
