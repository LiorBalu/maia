import { Navigate, Outlet, useLocation, useParams } from 'react-router-dom'

import { NoTenantPage } from '@/pages/NoTenantPage'

import { useAuth } from './AuthContext'

/** Anything under here needs a valid session. */
export function RequireAuth() {
  const { user } = useAuth()
  const location = useLocation()
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />
  return <Outlet />
}

/** Admin screens: staff only. */
export function RequireStaff() {
  const { user } = useAuth()
  if (!user?.isStaff) return <Navigate to="/" replace />
  return <Outlet />
}

/** Tenant screens: staff may enter any tenant, a member only the tenants in the token. */
export function RequireTenantAccess() {
  const { user } = useAuth()
  const { tenantName = '' } = useParams()
  if (!user) return <Navigate to="/login" replace />
  if (!user.isStaff && !user.tenants.includes(tenantName)) return <Navigate to="/" replace />
  return <Outlet />
}

/** "/" sends each role to its home. */
export function HomeRedirect() {
  const { user } = useAuth()
  if (!user) return <Navigate to="/login" replace />
  if (user.isStaff) return <Navigate to="/admin/tenants" replace />
  if (user.tenants.length > 0) return <Navigate to={`/s/${encodeURIComponent(user.tenants[0])}/inbox`} replace />
  return <NoTenantPage />
}
