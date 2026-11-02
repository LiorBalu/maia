import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

import { GPT_VOICES, getCallSettings, updateCallSettings, type CallSettings } from '@/api/settings'
import { Field, InlineError, InlineNotice, SaveButton, SectionCard, useSavedFlash } from '@/components/Form'
import { ErrorState } from '@/components/States'
import { errorDetail } from '@/lib/errors'
import { RangeField } from '@/pages/tenant/bot/ModelTab'

/** The phone channel: the business number and the OpenAI Realtime voice / listening settings. */
export function PhoneTab({ tenant }: { tenant: string }) {
  const { t } = useTranslation()
  const settings = useQuery({ queryKey: ['call-settings', tenant], queryFn: () => getCallSettings(tenant) })

  if (settings.isLoading) return <div className="card p-6 text-sm text-ink-muted">{t('common.loading')}</div>
  if (settings.isError || !settings.data) return <ErrorState onRetry={() => void settings.refetch()} />

  return (
    <div className="max-w-3xl">
      <CallSettingsForm tenant={tenant} settings={settings.data} />
    </div>
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
