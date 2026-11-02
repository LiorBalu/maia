import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

import { getChatSettings, getTenantDetail, updateChatSettings, updateTenant, type ChatSettings, type TenantDetail, type TenantFeature } from '@/api/settings'
import { Field, InlineError, SaveButton, SectionCard, Toggle, useSavedFlash } from '@/components/Form'
import { ErrorState } from '@/components/States'
import { errorDetail } from '@/lib/errors'

const LANGUAGES = ['he', 'en', 'ar', 'ru']

export function GeneralTab({ tenant }: { tenant: string }) {
  const { t } = useTranslation()
  const detail = useQuery({ queryKey: ['tenant-detail', tenant], queryFn: () => getTenantDetail(tenant) })
  const chat = useQuery({ queryKey: ['chat-settings', tenant], queryFn: () => getChatSettings(tenant) })

  if (detail.isLoading || chat.isLoading) return <div className="card p-6 text-sm text-ink-muted">{t('common.loading')}</div>
  if (detail.isError || !detail.data) return <ErrorState onRetry={() => void detail.refetch()} />
  if (chat.isError || !chat.data) return <ErrorState onRetry={() => void chat.refetch()} />

  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <BusinessCard tenant={tenant} detail={detail.data} />
      <ChatBehaviorCard tenant={tenant} settings={chat.data} detail={detail.data} />
    </div>
  )
}

interface BusinessForm {
  description: string
  default_language: string
  manual_mode: boolean
}

function BusinessCard({ tenant, detail }: { tenant: string; detail: TenantDetail }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const initial: BusinessForm = {
    description: detail.description ?? '',
    default_language: detail.default_language ?? 'he',
    manual_mode: Boolean(detail.manual_mode),
  }
  const [form, setForm] = useState(initial)
  useEffect(() => setForm(initial), [detail.description, detail.default_language, detail.manual_mode]) // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = JSON.stringify(form) !== JSON.stringify(initial)

  const save = useMutation({
    mutationFn: () => updateTenant(tenant, form),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['tenant-detail', tenant] })
      void queryClient.invalidateQueries({ queryKey: ['tenant', tenant] })
      void queryClient.invalidateQueries({ queryKey: ['tenants'] })
    },
  })
  const saved = useSavedFlash(save.isSuccess ? save.submittedAt : 0)

  return (
    <SectionCard title={t('settings.general.businessTitle')} subtitle={t('settings.general.businessSubtitle', { tenant })}>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (dirty) save.mutate()
        }}
        className="space-y-4"
      >
        <Field label={t('settings.general.description')} help={t('settings.general.descriptionHelp')}>
          <input className="input" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} dir="auto" />
        </Field>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t('common.language')} help={t('settings.general.languageHelp')}>
            <select className="input" value={form.default_language} onChange={(e) => setForm({ ...form, default_language: e.target.value })}>
              {LANGUAGES.map((l) => (
                <option key={l} value={l}>
                  {t(`settings.general.languages.${l}`)}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Toggle checked={form.manual_mode} onChange={(v) => setForm({ ...form, manual_mode: v })} label={t('settings.general.manualMode')} help={t('settings.general.manualModeHelp')} />
        <div className="flex items-center justify-between gap-3">
          <InlineError message={save.isError ? t('form.saveFailed', { reason: errorDetail(save.error) }) : null} />
          <SaveButton saving={save.isPending} saved={saved} disabled={!dirty} />
        </div>
      </form>
    </SectionCard>
  )
}

function featureEnabled(features: TenantFeature[] | null | undefined, name: string, fallback: boolean): boolean {
  const f = features?.find((x) => x.name === name)
  return f ? Boolean(f.enabled) : fallback
}

function ChatBehaviorCard({ tenant, settings, detail }: { tenant: string; settings: ChatSettings; detail: TenantDetail }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const initial = {
    chatbot_human_like_delay: Boolean(settings.chatbot_human_like_delay),
    chatbot_human_like_time: Number(settings.chatbot_human_like_time ?? 0),
    call_auto_reset: featureEnabled(detail.features, 'call_auto_reset', true),
  }
  const [form, setForm] = useState(initial)
  useEffect(() => setForm(initial), [settings, detail.features]) // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = JSON.stringify(form) !== JSON.stringify(initial)

  const save = useMutation({
    mutationFn: async () => {
      // The stored chat features (e.g. image_to_text) are sent back untouched: no screen edits them.
      await updateChatSettings(tenant, {
        ...settings,
        chatbot_human_like_delay: form.chatbot_human_like_delay,
        chatbot_human_like_time: form.chatbot_human_like_time,
      })
      if (form.call_auto_reset !== initial.call_auto_reset) {
        const tenantFeatures = (detail.features ?? []).map((f) =>
          f.name === 'call_auto_reset' ? { ...f, enabled: form.call_auto_reset, meta_data: f.meta_data ?? { on_start: true, on_end: true } } : f,
        )
        if (!tenantFeatures.some((f) => f.name === 'call_auto_reset'))
          tenantFeatures.push({ name: 'call_auto_reset', enabled: form.call_auto_reset, meta_data: { on_start: true, on_end: true } })
        await updateTenant(tenant, { features: tenantFeatures })
      }
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['chat-settings', tenant] })
      void queryClient.invalidateQueries({ queryKey: ['tenant-detail', tenant] })
    },
  })
  const saved = useSavedFlash(save.isSuccess ? save.submittedAt : 0)

  return (
    <SectionCard title={t('settings.general.behaviorTitle')} subtitle={t('settings.general.behaviorSubtitle')}>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (dirty) save.mutate()
        }}
        className="space-y-2"
      >
        <Toggle checked={form.chatbot_human_like_delay} onChange={(v) => setForm({ ...form, chatbot_human_like_delay: v })} label={t('settings.general.humanDelay')} help={t('settings.general.humanDelayHelp')} />
        {form.chatbot_human_like_delay && (
          <Field label={t('settings.general.humanDelayTime')}>
            <input type="number" min={0} max={60} className="input w-32" value={form.chatbot_human_like_time} onChange={(e) => setForm({ ...form, chatbot_human_like_time: Number(e.target.value) })} />
          </Field>
        )}
        <Toggle checked={form.call_auto_reset} onChange={(v) => setForm({ ...form, call_auto_reset: v })} label={t('settings.general.callAutoReset')} help={t('settings.general.callAutoResetHelp')} />
        <div className="flex items-center justify-between gap-3 pt-2">
          <InlineError message={save.isError ? t('form.saveFailed', { reason: errorDetail(save.error) }) : null} />
          <SaveButton saving={save.isPending} saved={saved} disabled={!dirty} />
        </div>
      </form>
    </SectionCard>
  )
}
