import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Building2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useMatch, useNavigate } from 'react-router-dom'

import { listTenants } from '@/api/tenants'
import { useAuth } from '@/auth/AuthContext'

/**
 * The business switch. Staff see every tenant in the system; a member sees the
 * tenants in their token. Switching keeps the current section (inbox, knowledge...).
 */
export function TenantSwitcher({ onNavigate }: { onNavigate?: () => void }) {
  const { t } = useTranslation()
  const { user } = useAuth()
  const navigate = useNavigate()
  const match = useMatch('/s/:tenantName/*')
  const current = match?.params.tenantName ?? ''
  const section = match?.params['*']?.split('/')[0] || 'inbox'

  const tenantsQuery = useQuery({
    queryKey: ['tenants'],
    queryFn: listTenants,
    enabled: Boolean(user?.isStaff),
  })

  const options = useMemo(() => {
    if (!user) return []
    if (user.isStaff) return (tenantsQuery.data ?? []).map((s) => s.name).sort((a, b) => a.localeCompare(b))
    return [...user.tenants]
  }, [user, tenantsQuery.data])

  if (!user) return null
  const loading = user.isStaff && tenantsQuery.isLoading
  const value = options.includes(current) ? current : ''

  return (
    <label className="block">
      <span className="label flex items-center gap-1.5">
        <Building2 size={14} />
        {t('switcher.label')}
      </span>
      <select
        className="input"
        value={value}
        disabled={loading}
        onChange={(event) => {
          const next = event.target.value
          if (!next) return
          navigate(`/s/${encodeURIComponent(next)}/${section}`)
          onNavigate?.()
        }}
      >
        <option value="" disabled>
          {loading ? t('switcher.loading') : t('switcher.placeholder')}
        </option>
        {options.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
    </label>
  )
}
