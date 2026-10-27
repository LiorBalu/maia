import { jwtDecode } from 'jwt-decode'

const TOKEN_KEY = 'maia_jwt'
const DEVICE_KEY = 'maia_device_id'

export type Role = 'staff' | 'partner' | 'user'

export interface SessionUser {
  email: string
  role: Role
  isStaff: boolean
  tenants: string[]
  permissions: Record<string, string[]>
  exp: number
}

interface Claims {
  sub: string
  is_staff?: boolean
  role?: Role
  tenants?: string[]
  permissions?: Record<string, string[]>
  exp: number
}

function safeGet(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function safeSet(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* private mode: the session simply does not survive a reload */
  }
}

export function getToken(): string | null {
  return safeGet(TOKEN_KEY)
}

export function setToken(token: string) {
  safeSet(TOKEN_KEY, token)
}

export function clearSession() {
  try {
    localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* ignore */
  }
}

/** Everything the UI knows about the user comes from the JWT claims. */
export function parseUser(token: string): SessionUser | null {
  try {
    const claims = jwtDecode<Claims>(token)
    if (!claims.sub || !claims.exp || claims.exp * 1000 < Date.now()) return null
    const isStaff = Boolean(claims.is_staff) || claims.role === 'staff'
    return {
      email: claims.sub,
      role: claims.role ?? (isStaff ? 'staff' : 'user'),
      isStaff,
      tenants: claims.tenants ?? [],
      permissions: claims.permissions ?? {},
      exp: claims.exp,
    }
  } catch {
    return null
  }
}

/** The backend wants a stable device id per browser for its device checks. */
export function getDeviceId(): string {
  let id = safeGet(DEVICE_KEY)
  if (!id) {
    id = typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : String(Date.now())
    safeSet(DEVICE_KEY, id)
  }
  return id
}
