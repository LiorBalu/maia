import { useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'

import { useAuth, type SignInError } from '@/auth/AuthContext'
import { Logo } from '@/components/Logo'

export function LoginPage() {
  const { t } = useTranslation()
  const { user, signIn } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<SignInError | null>(null)
  const [busy, setBusy] = useState(false)

  if (user) return <Navigate to="/" replace />

  const from = (location.state as { from?: string } | null)?.from

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await signIn(email.trim().toLowerCase(), password)
      navigate(from && from !== '/login' ? from : '/', { replace: true })
    } catch (err) {
      setError(((err as Error).message as SignInError) || 'failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-screen bg-gradient-to-b from-brand-100 via-surface to-white">
      <div className="mx-auto flex min-h-screen w-full max-w-md flex-col justify-center px-4 py-10">
        <div className="mb-8 flex justify-center">
          <Logo size="lg" />
        </div>
        <form onSubmit={onSubmit} className="card space-y-4 p-6 sm:p-8" noValidate>
          <div>
            <h1 className="text-xl font-bold text-ink">{t('login.title')}</h1>
            <p className="mt-1 text-sm text-ink-soft">{t('login.subtitle')}</p>
          </div>
          <div>
            <label className="label" htmlFor="email">
              {t('login.email')}
            </label>
            <input
              id="email"
              type="email"
              dir="ltr"
              className="input text-left"
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </div>
          <div>
            <label className="label" htmlFor="password">
              {t('login.password')}
            </label>
            <input
              id="password"
              type="password"
              dir="ltr"
              className="input text-left"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          {error && (
            <div role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-700 ring-1 ring-red-200">
              {t(`login.${error}`)}
            </div>
          )}
          <button type="submit" className="btn-primary w-full" disabled={busy || !email || !password}>
            {busy ? t('login.submitting') : t('login.submit')}
          </button>
        </form>
      </div>
    </div>
  )
}
