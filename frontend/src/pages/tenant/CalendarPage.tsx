import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { Link2, Unplug } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useParams } from 'react-router-dom'

import {
  DAY_KEYS,
  DEFAULT_OPENING_HOURS,
  disconnectGoogle,
  getGoogleAuthorizationUrl,
  listGoogleAccountDetails,
  listGoogleConnections,
  updateGoogleCalendar,
  type GoogleAccountDetail,
  type OpeningHours,
} from '@/api/calendar'
import { InlineError, InlineNotice, SaveButton, SectionCard, Toggle, useSavedFlash } from '@/components/Form'
import { PageHeader } from '@/components/PageHeader'
import { ErrorState } from '@/components/States'
import { useGooglePopup } from '@/hooks/useGooglePopup'
import { errorDetail } from '@/lib/errors'
import { CalendarBoard } from './calendar/CalendarBoard'

export function CalendarPage() {
  const { t } = useTranslation()
  const { tenantName: tenant = '' } = useParams()
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const connections = useQuery({ queryKey: ['google-connections', tenant, 'calendar'], queryFn: () => listGoogleConnections(tenant, 'calendar') })
  const details = useQuery({ queryKey: ['google-accounts', tenant], queryFn: () => listGoogleAccountDetails(tenant) })

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['google-connections', tenant] })
    void queryClient.invalidateQueries({ queryKey: ['google-accounts', tenant] })
    void queryClient.invalidateQueries({ queryKey: ['events', tenant] })
  }

  const popup = useGooglePopup((result) => {
    if (result.ok) {
      setError(null)
      setNotice(result.warning ? t('calendar.connectWarning', { reason: result.warning }) : null)
      refresh()
    } else if (result.message === 'popup-blocked') setError(t('calendar.popupBlocked'))
    else if (result.message !== 'closed') setError(t('calendar.connectFailed', { reason: result.message }))
  })

  const connect = useMutation({
    mutationFn: () => getGoogleAuthorizationUrl(tenant, 'calendar'),
    onSuccess: (url) => popup.open(url),
    onError: (e) => setError(t('calendar.connectFailed', { reason: errorDetail(e) })),
  })
  const disconnect = useMutation({
    mutationFn: (email: string) => disconnectGoogle(tenant, email, 'calendar'),
    onSuccess: refresh,
    onError: (e) => setError(t('calendar.disconnectFailed', { reason: errorDetail(e) })),
  })

  const calendars = connections.data ?? []
  const defaultEmail = calendars.find((c) => c.default)?.email ?? calendars[0]?.email

  return (
    <>
      <PageHeader title={t('pages.calendar.title')} subtitle={t('pages.calendar.desc')} />
      <div className="space-y-4">
        {calendars.length > 0 && <CalendarBoard tenant={tenant} email={defaultEmail} />}

        <SectionCard
          title={t('calendar.connectTitle')}
          subtitle={t('calendar.connectSubtitle')}
          actions={
            <button type="button" className="btn-primary" onClick={() => connect.mutate()} disabled={connect.isPending || popup.busy}>
              <Link2 size={16} />
              {popup.busy ? t('calendar.connecting') : t('calendar.connect')}
            </button>
          }
        >
          <InlineError message={error} />
          {notice && <InlineNotice>{notice}</InlineNotice>}
          {connections.isLoading && <div className="text-sm text-ink-muted">{t('common.loading')}</div>}
          {connections.isError && <ErrorState onRetry={() => void connections.refetch()} />}
          {connections.data && calendars.length === 0 && <InlineNotice>{t('calendar.none')}</InlineNotice>}
          {calendars.length > 0 && (
            <ul className="divide-y divide-brand-50">
              {calendars.map((c) => (
                <li key={c.email} className="flex flex-wrap items-center gap-3 py-2">
                  <span className="font-medium text-ink" dir="ltr">
                    {c.email}
                  </span>
                  {c.default && <span className="badge bg-brand-100 text-brand-800">{t('calendar.default')}</span>}
                  {c.scopes.some((s) => s.includes('gmail')) && <span className="badge bg-slate-100 text-slate-600">{t('calendar.alsoGmail')}</span>}
                  <button
                    type="button"
                    className="btn-ghost ms-auto text-xs text-red-600 hover:bg-red-50"
                    disabled={disconnect.isPending}
                    onClick={() => {
                      if (window.confirm(t('calendar.disconnectConfirm', { email: c.email }))) disconnect.mutate(c.email)
                    }}
                  >
                    <Unplug size={14} />
                    {t('calendar.disconnect')}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </SectionCard>

        {(details.data ?? [])
          .filter((d) => calendars.some((c) => c.email === d.email))
          .map((account) => (
            <OpeningHoursCard key={account.email} tenant={tenant} account={account} />
          ))}
      </div>
    </>
  )
}

function normalizeHours(raw: Partial<OpeningHours>[] | undefined): OpeningHours[] {
  return DAY_KEYS.map((day) => {
    const found = raw?.find((h) => h.day === day)
    const fallback = DEFAULT_OPENING_HOURS.find((h) => h.day === day)!
    return {
      day,
      start_time: found?.start_time || fallback.start_time,
      end_time: found?.end_time || fallback.end_time,
      closed: found?.closed ?? fallback.closed,
    }
  })
}

function OpeningHoursCard({ tenant, account }: { tenant: string; account: GoogleAccountDetail }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const initial = useMemo(() => normalizeHours(account.opening_hours), [account.opening_hours])
  const [hours, setHours] = useState<OpeningHours[]>(initial)
  const [ignoreLong, setIgnoreLong] = useState(Boolean(account.ignore_long_events))
  useEffect(() => {
    setHours(initial)
    setIgnoreLong(Boolean(account.ignore_long_events))
  }, [initial, account.ignore_long_events])
  const dirty = JSON.stringify(hours) !== JSON.stringify(initial) || ignoreLong !== Boolean(account.ignore_long_events)

  const save = useMutation({
    mutationFn: () => updateGoogleCalendar(tenant, account.email, { opening_hours: hours, ignore_long_events: ignoreLong }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['google-accounts', tenant] }),
  })
  const saved = useSavedFlash(save.isSuccess ? save.submittedAt : 0)
  const update = (day: string, patch: Partial<OpeningHours>) => setHours((h) => h.map((row) => (row.day === day ? { ...row, ...patch } : row)))

  return (
    <SectionCard title={t('calendar.hoursTitle', { email: account.email })} subtitle={t('calendar.hoursSubtitle')}>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (dirty) save.mutate()
        }}
      >
        <ul className="divide-y divide-brand-50">
          {hours.map((row) => (
            <li key={row.day} className="flex flex-wrap items-center gap-3 py-2">
              <span className="w-20 text-sm font-medium text-ink">{t(`calendar.days.${row.day}`)}</span>
              <label className="flex items-center gap-1.5 text-xs text-ink-soft">
                <input type="checkbox" className="accent-brand-600" checked={!row.closed} onChange={(e) => update(row.day, { closed: !e.target.checked })} />
                {t('calendar.open')}
              </label>
              <input type="time" dir="ltr" className={clsx('input w-28', row.closed && 'opacity-40')} disabled={row.closed} value={row.start_time} onChange={(e) => update(row.day, { start_time: e.target.value })} />
              <span className="text-xs text-ink-muted">{t('calendar.to')}</span>
              <input type="time" dir="ltr" className={clsx('input w-28', row.closed && 'opacity-40')} disabled={row.closed} value={row.end_time} onChange={(e) => update(row.day, { end_time: e.target.value })} />
              {row.closed && <span className="text-xs text-ink-muted">{t('calendar.closed')}</span>}
            </li>
          ))}
        </ul>
        <Toggle checked={ignoreLong} onChange={setIgnoreLong} label={t('calendar.ignoreLong')} help={t('calendar.ignoreLongHelp')} />
        <div className="mt-3 flex items-center justify-between gap-3">
          <InlineError message={save.isError ? t('form.saveFailed', { reason: errorDetail(save.error) }) : null} />
          <SaveButton saving={save.isPending} saved={saved} disabled={!dirty} />
        </div>
      </form>
    </SectionCard>
  )
}
