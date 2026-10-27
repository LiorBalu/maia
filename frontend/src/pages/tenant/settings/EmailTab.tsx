import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link2, Play, RefreshCw, Square, Unplug } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { disconnectGoogle, getGoogleAuthorizationUrl, listGoogleAccountDetails, listGoogleConnections } from '@/api/calendar'
import { getEmailWatchStatus, registerEmailStream, unregisterEmailStream, type EmailWatchStatus } from '@/api/settings'
import { InlineError, InlineNotice, SectionCard } from '@/components/Form'
import { ErrorState } from '@/components/States'
import { useGooglePopup } from '@/hooks/useGooglePopup'
import { errorDetail } from '@/lib/errors'

export function EmailTab({ tenant }: { tenant: string }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [statuses, setStatuses] = useState<EmailWatchStatus[] | null>(null)

  const connections = useQuery({ queryKey: ['google-connections', tenant, 'email'], queryFn: () => listGoogleConnections(tenant, 'email') })
  const details = useQuery({ queryKey: ['google-accounts', tenant], queryFn: () => listGoogleAccountDetails(tenant) })
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['google-connections', tenant] })
    void queryClient.invalidateQueries({ queryKey: ['google-accounts', tenant] })
  }

  const popup = useGooglePopup((result) => {
    if (result.ok) {
      setError(null)
      setNotice(
        result.warning ? t('settings.email.connectedWithWarning', { reason: result.warning }) : t('settings.email.connectedNotice'),
      )
      refresh()
    } else if (result.message === 'popup-blocked') setError(t('calendar.popupBlocked'))
    else if (result.message !== 'closed') setError(t('calendar.connectFailed', { reason: result.message }))
  })
  const connect = useMutation({
    mutationFn: () => getGoogleAuthorizationUrl(tenant, 'email', true),
    onSuccess: (url) => popup.open(url),
    onError: (e) => setError(errorDetail(e)),
  })
  const register = useMutation({
    mutationFn: (email: string) => registerEmailStream(tenant, email),
    onSuccess: (res) => {
      setError(null)
      setNotice(res.message || t('settings.email.started'))
      refresh()
    },
    onError: (e) => setError(errorDetail(e)),
  })
  const unregister = useMutation({
    mutationFn: (email: string) => unregisterEmailStream(tenant, email),
    onSuccess: (res) => {
      setNotice(res.message || t('settings.email.stopped'))
      refresh()
    },
    onError: (e) => setError(errorDetail(e)),
  })
  const disconnect = useMutation({
    mutationFn: (email: string) => disconnectGoogle(tenant, email, 'email'),
    onSuccess: refresh,
    onError: (e) => setError(errorDetail(e)),
  })
  const check = useMutation({
    mutationFn: () => getEmailWatchStatus(tenant),
    onSuccess: (rows) => setStatuses(rows),
    onError: (e) => setError(errorDetail(e)),
  })

  const accounts = connections.data ?? []

  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <SectionCard
        title={t('settings.email.title')}
        subtitle={t('settings.email.subtitle')}
        actions={
          <button type="button" className="btn-primary" onClick={() => connect.mutate()} disabled={connect.isPending || popup.busy}>
            <Link2 size={16} />
            {popup.busy ? t('calendar.connecting') : t('settings.email.connect')}
          </button>
        }
      >
        <div className="space-y-3">
          <InlineError message={error} />
          {notice && <InlineNotice>{notice}</InlineNotice>}
          {connections.isLoading && <div className="text-sm text-ink-muted">{t('common.loading')}</div>}
          {connections.isError && <ErrorState onRetry={() => void connections.refetch()} />}
          {connections.data && accounts.length === 0 && <InlineNotice>{t('settings.email.none')}</InlineNotice>}
          {accounts.length > 0 && (
            <ul className="divide-y divide-brand-50">
              {accounts.map((c) => {
                const detail = details.data?.find((d) => d.email === c.email)
                const watching = Boolean(detail?.gmail_watch_enabled)
                return (
                  <li key={c.email} className="flex flex-wrap items-center gap-2 py-2">
                    <span className="font-medium text-ink" dir="ltr">
                      {c.email}
                    </span>
                    <span className={watching ? 'badge bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200' : 'badge bg-amber-50 text-amber-700 ring-1 ring-amber-200'}>
                      {watching ? t('settings.email.watching') : t('settings.email.notWatching')}
                    </span>
                    <span className="ms-auto flex flex-wrap gap-1">
                      <button type="button" className="btn-soft text-xs" onClick={() => register.mutate(c.email)} disabled={register.isPending}>
                        <Play size={14} />
                        {t('settings.email.start')}
                      </button>
                      <button type="button" className="btn-ghost text-xs" onClick={() => unregister.mutate(c.email)} disabled={unregister.isPending}>
                        <Square size={14} />
                        {t('settings.email.stop')}
                      </button>
                      <button
                        type="button"
                        className="btn-ghost text-xs text-red-600 hover:bg-red-50"
                        disabled={disconnect.isPending}
                        onClick={() => {
                          if (window.confirm(t('calendar.disconnectConfirm', { email: c.email }))) disconnect.mutate(c.email)
                        }}
                      >
                        <Unplug size={14} />
                        {t('calendar.disconnect')}
                      </button>
                    </span>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </SectionCard>

      <SectionCard
        title={t('settings.email.statusTitle')}
        subtitle={t('settings.email.statusSubtitle')}
        actions={
          <button type="button" className="btn-soft" onClick={() => check.mutate()} disabled={check.isPending}>
            <RefreshCw size={14} className={check.isPending ? 'animate-spin' : undefined} />
            {t('settings.email.check')}
          </button>
        }
      >
        {statuses === null && !check.isPending && <p className="text-sm text-ink-muted">{t('settings.email.statusHint')}</p>}
        {check.isPending && <p className="text-sm text-ink-muted">{t('common.loading')}</p>}
        {statuses && statuses.length === 0 && <p className="text-sm text-ink-muted">{t('settings.email.none')}</p>}
        {statuses && statuses.length > 0 && (
          <ul className="space-y-2">
            {statuses.map((s) => (
              <li key={s.email} className="rounded-xl bg-surface p-3 text-sm">
                <div className="font-medium text-ink" dir="ltr">
                  {s.email}
                </div>
                <div className="mt-1 text-xs text-ink-soft">
                  {s.watch ? (
                    <span className={s.watch.is_expired ? 'text-amber-700' : 'text-emerald-700'}>
                      {s.watch.is_expired ? t('settings.email.expired') : t('settings.email.activeUntil', { when: s.watch.expiration_human ?? '' })}
                    </span>
                  ) : (
                    <span>{t('settings.email.noWatch')}</span>
                  )}
                </div>
                {s.errors && s.errors.length > 0 && (
                  <ul className="mt-1 list-disc ps-5 text-xs text-red-700">
                    {s.errors.map((e, i) => (
                      <li key={i} dir="ltr" className="text-left">
                        {e}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        )}
      </SectionCard>
    </div>
  )
}
