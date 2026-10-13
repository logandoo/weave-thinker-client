import axios from 'axios'
import { clearStoredAuth } from '@/composables/useAuth'
import { apiUrl } from './base'

export const TOKEN_KEY = 'chatllm_token'

// /auth/refresh is intentionally skipped: a 401 there means the token
// already expired OR a concurrent tab rotated it first — either way the
// normal 401 flow will catch it on the next real API call, and force-
// redirecting here would kick the user out on a harmless race.
const AUTH_SKIP_URLS = ['/auth/login', '/auth/register', '/auth/logout', '/auth/refresh']

const api = axios.create({
  baseURL: apiUrl('/api'),
  headers: {
    'Content-Type': 'application/json'
  }
})

api.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      const requestUrl = error.config?.url || ''
      const isAuthEndpoint = AUTH_SKIP_URLS.some(url => requestUrl.includes(url))

      if (!isAuthEndpoint && !window.location.pathname.includes('/login')) {
        clearStoredAuth()
        window.location.href = '/login?expired=1'
        return Promise.reject(error)
      }
    }
    return Promise.reject(error)
  }
)

export default api
