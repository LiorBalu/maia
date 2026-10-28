import { useEffect, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import {
  GPT_VOICES,
  getCallSettings,
  getTenantDetail,
  updateCallSettings,
  updateTenant,
  type CallSettings,
  type TwilioAccountInput,
} from '@/api/settings'
import { Field, InlineError, InlineNotice, SaveButton, SectionCard, Toggle, useSavedFlash } from '@/components/Form'
import { ErrorState } from '@/components/States'
import { errorDetail } from '@/lib/errors'
import { RangeField } from '@/pages/tenant/bot/ModelTab'

import { WebhookCard } from './WebhookCard'

export function PhoneTab({ tenant }: { tenant: string }) {
  const { t } = useTranslation()
  const settings = useQuery({ queryKey: ['call-settings', tenant], queryFn: () => getCallSettings(tenant) })

  if (settings.isLoading) return <div className="card p-6 text-sm text-ink-muted">{t('common.loading')}</div>
  if (settings.isError || !settings.data) return <ErrorState onRetry={() => void settings.refetch()} />

  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <CallSettingsForm tenant={tenant} settings={settings.data} />
      <div className="space-y-4">
        <WebhookCard
          title={t('settings.phone.webhooksTitle')}
          subtitle={t('settings.phone.webhooksSubtitle')}
          items={[{ label: t('settings.phone.voiceUrl'), path: `/api/v1/telephone/${tenant}`, help: t('settings.phone.voiceHelp') }]}
        />
        <TwilioCard tenant={tenant} />
      </div>
    </div>
  )
}

const emptyTwilio = (): TwilioAccountInput => ({ account_sid: '', auth_token: '', phone_number: '' })

/** Twilio Voice credentials: only needed to fetch the call recording after a call ends. */
function TwilioCard({ tenant }: { tenant: string }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const detail = useQuery({ queryKey: ['tenant-detail', tenant], queryFn: () => getTenantDetail(tenant) })
  const existing = detail.data?.twilio_accounts?.[0]
  const initial: TwilioAccountInput = existing
    ? {
        account_sid: existing.account_sid ?? '',
        auth_token: (existing as { auth_token?: string }).auth_token ?? '',
        phone_number: existing.phone_number ?? '',
      }
    : emptyTwilio()
  const [form, setForm] = useState<TwilioAccountInput>(initial)
  useEffect(() => setForm(initial), [detail.data]) // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = JSON.stringify(form) !== JSON.stringify(initial)

  const save = useMutation({
    mutationFn: (accounts: TwilioAccountInput[]) => updateTenant(tenant, { twilio_accounts: accounts }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['tenant-detail', tenant] })
      void queryClient.invalidateQueries({ queryKey: ['tenant', tenant] })
    },
  })
  const saved = useSavedFlash(save.isSuccess ? save.submittedAt : 0)

  function submit(e: FormEvent) {
    e.preventDefault()
    save.mutate([
      {
        ...form,
        account_sid: form.account_sid.trim(),
        auth_token: form.auth_token.trim(),
        phone_number: form.phone_number.trim(),
      },
    ])
  }

  if (detail.isLoading) return <div className="card p-6 text-sm text-ink-muted">{t('common.loading')}</div>
  if (detail.isError) return <ErrorState onRetry={() => void detail.refetch()} />

  const set = <K extends keyof TwilioAccountInput>(key: K, value: TwilioAccountInput[K]) => setForm((f) => ({ ...f, [key]: value }))

  return (
    <SectionCard
      title={t('settings.phone.twilioTitle')}
      subtitle={t('settings.phone.twilioSubtitle')}
      actions={
        existing ? <span className="badge bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200">{t('settings.connected')}</span> : <span className="badge bg-slate-100 text-slate-600">{t('settings.notConnected')}</span>
      }
    >
      <form onSubmit={submit} className="space-y-3">
        <Field label={t('settings.phone.accountSid')}>
          <input className="input text-left font-mono text-xs" dir="ltr" value={form.account_sid} onChange={(e) => set('account_sid', e.target.value)} placeholder="ACxxxxxxxx" required />
        </Field>
        <Field label={t('settings.phone.authToken')} help={t('settings.phone.authTokenHelp')}>
          <input type="password" className="input text-left font-mono text-xs" dir="ltr" value={form.auth_token} onChange={(e) => set('auth_token', e.target.value)} autoComplete="new-password" required />
        </Field>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t('settings.phone.twilioPhone')} help={t('settings.phone.twilioPhoneHelp')}>
            <input className="input text-left" dir="ltr" value={form.phone_number} onChange={(e) => set('phone_number', e.target.value)} placeholder="+972..." required />
          </Field>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
          <InlineError message={save.isError ? t('form.saveFailed', { reason: errorDetail(save.error) }) : null} />
          <div className="flex gap-2">
            {existing && (
              <button
                type="button"
                className="btn-ghost text-red-600 hover:bg-red-50"
                disabled={save.isPending}
                onClick={() => {
                  if (window.confirm(t('settings.phone.removeConfirm'))) save.mutate([])
                }}
              >
                <Trash2 size={14} />
                {t('settings.phone.remove')}
              </button>
            )}
            <SaveButton saving={save.isPending} saved={saved} disabled={!dirty} />
          </div>
        </div>
      </form>
    </SectionCard>
  )
}

function CallSettingsForm({ tenant, settings }: { tenant: string; settings: CallSettings }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [form, setForm] = useState<CallSettings>(settings)
  useEffect(() => setForm(settings), [settings])
  const dirty = JSON.stringify(form) !== JSON.stringify(settings)

  const save = useMutation({
    mutationFn: () => updateCallSettings(tenant, { ...form, tts_provider: 'realtime' }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['call-settings', tenant] }),
  })
  const saved = useSavedFlash(save.isSuccess ? save.submittedAt : 0)

  const conf = form.realtime_conf
  const setConf = (patch: Partial<CallSettings['realtime_conf']>) => setForm((f) => ({ ...f, realtime_conf: { ...f.realtime_conf, ...patch } }))
  const noise = form.features?.find((f) => f.name === 'noise_suppression')
  const setNoise = (enabled: boolean) =>
    setForm((f) => ({
      ...f,
      features: (f.features ?? []).some((x) => x.name === 'noise_suppression')
        ? (f.features ?? []).map((x) => (x.name === 'noise_suppression' ? { ...x, enabled } : x))
        : [...(f.features ?? []), { name: 'noise_suppression', enabled, meta_data: {} }],
    }))

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        if (dirty) save.mutate()
      }}
      className="space-y-4"
    >
      <SectionCard title={t('settings.phone.numbersTitle')} subtitle={t('settings.phone.numbersSubtitle')}>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t('settings.phone.number')} help={t('settings.phone.numberHelp')}>
            <input className="input text-left" dir="ltr" value={form.phone_number} onChange={(e) => setForm({ ...form, phone_number: e.target.value })} placeholder="+972..." />
          </Field>
          <Field label={t('settings.phone.representative')} help={t('settings.phone.representativeHelp')}>
            <input className="input text-left" dir="ltr" value={form.representative_phone_number} onChange={(e) => setForm({ ...form, representative_phone_number: e.target.value })} placeholder="+972..." />
          </Field>
        </div>
      </SectionCard>

      <SectionCard title={t('settings.phone.voiceTitle')} subtitle={t('settings.phone.voiceSubtitle')}>
        <div className="space-y-4">
          <Field label={t('settings.phone.voice')} help={t('settings.phone.voiceHelp2')}>
            <select className="input w-48" value={form.gpt_voice} onChange={(e) => setForm({ ...form, gpt_voice: e.target.value })}>
              {[...new Set([form.gpt_voice, ...GPT_VOICES])].filter(Boolean).map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
          </Field>
          <RangeField label={t('settings.phone.speed')} help={t('settings.phone.speedHelp')} value={form.openai_speed} min={0.5} max={1.5} step={0.05} onChange={(v) => setForm({ ...form, openai_speed: v })} />
        </div>
      </SectionCard>

      <SectionCard title={t('settings.phone.listeningTitle')} subtitle={t('settings.phone.listeningSubtitle')}>
        <div className="space-y-4">
          <RangeField label={t('settings.phone.threshold')} help={t('settings.phone.thresholdHelp')} value={conf.threshold} min={0} max={1} step={0.05} onChange={(v) => setConf({ threshold: v })} />
          <RangeField
            label={t('settings.phone.silence')}
            help={t('settings.phone.silenceHelp')}
            value={conf.silence_duration_ms}
            min={200}
            max={2000}
            step={50}
            format={(v) => `${v} ms`}
            onChange={(v) => setConf({ silence_duration_ms: v })}
          />
          <RangeField
            label={t('settings.phone.prefix')}
            help={t('settings.phone.prefixHelp')}
            value={conf.prefix_padding_ms}
            min={0}
            max={2000}
            step={50}
            format={(v) => `${v} ms`}
            onChange={(v) => setConf({ prefix_padding_ms: v })}
          />
          <Field label={t('settings.phone.noiseMode')} help={t('settings.phone.noiseModeHelp')}>
            <select className="input w-48" value={conf.noise_reduction_mode ?? 'near_field'} onChange={(e) => setConf({ noise_reduction_mode: e.target.value })}>
              <option value="near_field">{t('settings.phone.noiseModes.near_field')}</option>
              <option value="far_field">{t('settings.phone.noiseModes.far_field')}</option>
              <option value="none">{t('settings.phone.noiseModes.none')}</option>
            </select>
          </Field>
          <Toggle checked={Boolean(noise?.enabled)} onChange={setNoise} label={t('settings.phone.noiseSuppression')} help={t('settings.phone.noiseSuppressionHelp')} />
          <InlineNotice>{t('settings.phone.realtimeNotice')}</InlineNotice>
        </div>
        <div className="mt-4 flex items-center justify-between gap-3">
          <InlineError message={save.isError ? t('form.saveFailed', { reason: errorDetail(save.error) }) : null} />
          <SaveButton saving={save.isPending} saved={saved} disabled={!dirty} />
        </div>
      </SectionCard>
    </form>
  )
}
