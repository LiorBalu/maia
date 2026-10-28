import { getDeviceId } from '@/auth/session'

import { api } from './client'

export interface LoginResponse {
  token?: string
  device_verified?: boolean
  message?: string
}

export async function login(email: string, password: string): Promise<LoginResponse> {
  const { data } = await api.post<LoginResponse>('/api/auth/login', {
    email,
    password,
    device_id: getDeviceId(),
    device_name: navigator.platform || 'browser',
  })
  return data
}

export type Role = 'staff' | 'user'

export interface ApiUser {
  email: string
  name?: string
  role?: Role
  is_staff?: boolean
  tenants_name?: string[]
  last_login?: string
}

export async function listUsers(): Promise<ApiUser[]> {
  const { data } = await api.get<ApiUser[]>('/api/auth/users')
  return data
}
