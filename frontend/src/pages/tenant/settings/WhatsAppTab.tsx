import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, QrCode, RefreshCw, Trash2, Unplug } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import {
  createWahaAccount,
  createWahaClient,
  deleteWahaAccount,
  disconnectWaha,
  getWahaQr,
  getWahaStatus,
  listWahaAccounts,
  type WahaAccount,
} from '@/api/settings'
import { Field, InlineError, InlineNotice, SectionCard } from '@/components/Form'
import { ErrorState } from '@/components/States'
import { errorDetail } from '@/lib/errors'

/** WhatsApp runs through WAHA only: a number is linked by scanning a QR code from the phone. */
export function WhatsAppTab({ tenant }: { tenant: string }) {
  return <WahaCard tenant={tenant} />
}

function WahaCard({ tenant }: { tenant: string }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [phone, setPhone] = useState('')
  const [error, setError] = useState<string | null>(null)
  const accounts = useQuery({ queryKey: ['waha-accounts', tenant], queryFn: () => listWahaAccounts(tenant) })
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['waha-accounts', tenant] })
    void queryClient.invalidateQueries({ queryKey: ['tenant', tenant] })
  }

  const add = useMutation({
    mutationFn: () => createWahaAccount(tenant, phone.trim()),
    onSuccess: () => {
      setPhone('')
      setError(null)
      invalidate()
    },
    onError: (e) => setError(errorDetail(e)),
  })

  return (
    <SectionCard title={t('settings.whatsapp.wahaTitle')} subtitle={t('settings.whatsapp.wahaSubtitle')}>
      <InlineNotice>{t('settings.whatsapp.wahaNotice')}</InlineNotice>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (phone.trim()) add.mutate()
        }}
        className="mt-3 flex flex-wrap items-end gap-2"
      >
        <Field label={t('settings.whatsapp.wahaPhone')} help={t('settings.whatsapp.wahaPhoneHelp')} className="min-w-[14rem]">
          <input className="input text-left" dir="ltr" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="972501234567" />
        </Field>
        <button type="submit" className="btn-primary" disabled={add.isPending || !phone.trim()}>
          <Plus size={16} />
          {t('settings.whatsapp.wahaAdd')}
        </button>
      </form>
      <div className="mt-3">
        <InlineError message={error} />
      </div>
      {accounts.isError && <ErrorState onRetry={() => void accounts.refetch()} />}
      {(accounts.data ?? []).length > 0 && (
        <ul className="mt-3 divide-y divide-brand-50">
          {accounts.data!.map((account) => (
            <WahaRow key={account.phone_number} tenant={tenant} account={account} onChanged={invalidate} onError={setError} />
          ))}
        </ul>
      )}
    </SectionCard>
  )
}

function WahaRow({ tenant, account, onChanged, onError }: { tenant: string; account: WahaAccount; onChanged: () => void; onError: (m: string) => void }) {
  const { t } = useTranslation()
  const [qr, setQr] = useState<string | null>(null)
  const [status, setStatus] = useState<string | null>(account.session_status ?? null)

  const start = useMutation({ mutationFn: () => createWahaClient(tenant, account.phone_number), onSuccess: onChanged, onError: (e) => onError(errorDetail(e)) })
  const fetchQr = useMutation({
    mutationFn: () => getWahaQr(tenant, account.phone_number),
    onSuccess: (data) => {
      if (data?.data) setQr(`data:${data.mimetype || 'image/png'};base64,${data.data}`)
      else if (typeof data?.qrCode === 'string') setQr(data.qrCode)
      else onError(t('settings.whatsapp.wahaNoQr'))
    },
    onError: (e) => onError(errorDetail(e)),
  })
  const refresh = useMutation({
    mutationFn: () => getWahaStatus(tenant, account.phone_number),
    onSuccess: (data) => {
      const s = (data as { status?: string; session_status?: string })?.status ?? (data as { session_status?: string })?.session_status
      if (s) setStatus(String(s))
      onChanged()
    },
    onError: (e) => onError(errorDetail(e)),
  })
  const disconnect = useMutation({ mutationFn: () => disconnectWaha(tenant, account.phone_number), onSuccess: onChanged, onError: (e) => onError(errorDetail(e)) })
  const remove = useMutation({ mutationFn: () => deleteWahaAccount(tenant, account.phone_number), onSuccess: onChanged, onError: (e) => onError(errorDetail(e)) })

  const working = status === 'WORKING' || account.is_connected
  return (
    <li className="py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm text-ink" dir="ltr">
          {account.phone_number}
        </span>
        <span className={working ? 'badge bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200' : 'badge bg-slate-100 text-slate-600'}>
          {status || t('settings.notConnected')}
        </span>
        <span className="ms-auto flex flex-wrap gap-1">
          <button type="button" className="btn-soft text-xs" onClick={() => start.mutate()} disabled={start.isPending}>
            {t('settings.whatsapp.wahaStart')}
          </button>
          <button type="button" className="btn-soft text-xs" onClick={() => fetchQr.mutate()} disabled={fetchQr.isPending}>
            <QrCode size={14} />
            {t('settings.whatsapp.wahaQr')}
          </button>
          <button type="button" className="btn-ghost text-xs" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
            <RefreshCw size={14} />
            {t('settings.whatsapp.wahaStatus')}
          </button>
          <button type="button" className="btn-ghost text-xs" onClick={() => disconnect.mutate()} disabled={disconnect.isPending}>
            <Unplug size={14} />
            {t('settings.whatsapp.wahaDisconnect')}
          </button>
          <button
            type="button"
            className="btn-ghost text-xs text-red-600 hover:bg-red-50"
            disabled={remove.isPending}
            onClick={() => {
              if (window.confirm(t('settings.whatsapp.wahaRemoveConfirm'))) remove.mutate()
            }}
          >
            <Trash2 size={14} />
          </button>
        </span>
      </div>
      {qr && (
        <div className="mt-3 flex flex-col items-center gap-2 rounded-2xl bg-white p-4 ring-1 ring-brand-100">
          <img src={qr} alt="QR" className="h-56 w-56" />
          <p className="text-xs text-ink-soft">{t('settings.whatsapp.wahaScan')}</p>
          <button type="button" className="btn-ghost text-xs" onClick={() => setQr(null)}>
            {t('common.close')}
          </button>
        </div>
      )}
    </li>
  )
}
