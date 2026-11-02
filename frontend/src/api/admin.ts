import { api } from './client'

/** Same policy as the backend (`maia/api/auth.py` PASSWORD_REGEX): 8+ chars, an uppercase letter, a special character. */
export const PASSWORD_RULE = /^(?=.*[A-Z])(?=.*[!@#$%^&*(),.?":{}|<>])[A-Za-z\d!@#$%^&*(),.?":{}|<>]{8,}$/

export interface CreateTenantInput {
  name: string
  description: string
  default_language: string
  /** The business owner's dashboard login, created together with the tenant (role `user`, member of it). */
  owner_email: string
  owner_password: string
}

/** Creates a tenant with its owner's login; the backend also seeds its prompts, agent and settings. */
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

/** The backend derives `is_staff` from `role` and gives a tenant member full access to its tenants by default. */
export async function createUser(input: CreateUserInput): Promise<void> {
  await api.post('/api/auth/register', {
    name: input.name,
    email: input.email,
    password: input.password,
    role: input.role,
    tenants_name: input.role === 'staff' ? [] : input.tenants,
  })
}

/** Lower-case latin letters, digits and dashes, the way the backend stores tenant names. */
export function toTenantSlug(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}
