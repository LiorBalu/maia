import { api } from './client'

const enc = encodeURIComponent

export interface CreateTenantInput {
  name: string
  description: string
  default_language: string
  default_timezone: string
}

/** Creates a tenant; the backend also seeds its prompts, agent and settings. */
export async function createTenant(input: CreateTenantInput): Promise<void> {
  await api.post('/management/v1/tenants', { ...input, status: 'active' })
}

export interface CreateUserInput {
  name: string
  email: string
  password: string
  role: 'staff' | 'user'
  tenants: string[]
}

export async function createUser(input: CreateUserInput): Promise<void> {
  const permissions = Object.fromEntries(input.tenants.map((s) => [s, ['grant_access', 'min_access']]))
  await api.post('/api/auth/register', {
    name: input.name,
    email: input.email,
    password: input.password,
    role: input.role,
    is_staff: input.role === 'staff',
    tenants_name: input.role === 'staff' ? [] : input.tenants,
    permissions: input.role === 'staff' ? {} : permissions,
  })
}

export async function deleteUser(email: string): Promise<void> {
  await api.delete(`/api/auth/users/${enc(email)}`)
}

/** Lower-case latin letters, digits and dashes, the way the backend stores tenant names. */
export function toTenantSlug(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}
