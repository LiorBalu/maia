import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

import { useAuth } from '@/auth/AuthContext'

export function NoTenantPage() {
  const { t } = useTranslation()
  const { signOut } = useAuth()
  const navigate = useNavigate()
  return (
    <div className="mx-auto max-w-lg">
      <div className="card p-8 text-center">
        <h1 className="text-xl font-bold text-ink">{t('noTenant.title')}</h1>
        <p className="mt-2 text-sm text-ink-soft">{t('noTenant.body')}</p>
        <button
          type="button"
          className="btn-soft mt-6"
          onClick={() => {
            signOut()
            navigate('/login', { replace: true })
          }}
        >
          {t('common.logout')}
        </button>
      </div>
    </div>
  )
}
