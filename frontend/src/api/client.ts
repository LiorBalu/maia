import axios from 'axios'

import { clearSession, getToken } from '@/auth/session'

// One axios instance for the whole app. Relative base URL in development
// (Vite proxies /api and /management to the backend), VITE_API_URL otherwise.
export const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '',
  headers: { 'Content-Type': 'application/json' },
})

api.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error?.response?.status
    const url = String(error?.config?.url ?? '')
    if (status === 401 && !url.includes('/api/auth/login')) {
      clearSession()
      if (window.location.pathname !== '/login') window.location.assign('/login')
    }
    return Promise.reject(error)
  },
)
