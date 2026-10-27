import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

import { MODEL_SUGGESTIONS, PROVIDERS, createAgent, listAgents, updateAgent, type Agent, type AgentInput } from '@/api/bot'
import { Field, InlineError, InlineNotice, SaveButton, SectionCard, useSavedFlash } from '@/components/Form'
import { ErrorState } from '@/components/States'
import { errorDetail } from '@/lib/errors'

interface FormState {
  provider: string
  model: string
  temperature: number
  score_threshold: number
  top_k_general: number
  top_k_qa: number
}

function fromAgent(agent?: Agent): FormState {
  return {
    provider: agent?.provider ?? 'openai',
    model: agent?.model ?? 'gpt-4.1',
    temperature: agent?.temperature ?? 0.3,
    score_threshold: agent?.score_threshold ?? 0.5,
    top_k_general: agent?.top_k_general ?? 10,
    top_k_qa: agent?.top_k_qa ?? 5,
  }
}

export function ModelTab({ tenant }: { tenant: string }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const agents = useQuery({ queryKey: ['agents', tenant], queryFn: () => listAgents(tenant) })
  const agent = agents.data?.find((a) => a.is_active) ?? agents.data?.[0]
  const [form, setForm] = useState<FormState>(() => fromAgent(agent))
  useEffect(() => setForm(fromAgent(agent)), [agent])
  const dirty = JSON.stringify(form) !== JSON.stringify(fromAgent(agent))

  const save = useMutation({
    mutationFn: () => {
      const input: AgentInput = {
        name: agent?.name ?? 'Default Agent',
        description: agent?.description ?? t('bot.model.defaultDescription'),
        is_active: true,
        direct_answer_threshold: agent?.direct_answer_threshold ?? 0,
        ...form,
        model: form.model.trim(),
      }
      return agent ? updateAgent(agent.id, input) : createAgent(tenant, input)
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['agents', tenant] }),
  })
  const saved = useSavedFlash(save.isSuccess ? save.submittedAt : 0)

  if (agents.isLoading) return <div className="card p-6 text-sm text-ink-muted">{t('common.loading')}</div>
  if (agents.isError) return <ErrorState onRetry={() => void agents.refetch()} />

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => setForm((f) => ({ ...f, [key]: value }))
  const suggestions = MODEL_SUGGESTIONS[form.provider] ?? []

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        if (dirty) save.mutate()
      }}
      className="grid gap-4 xl:grid-cols-2"
    >
      <SectionCard title={t('bot.model.title')} subtitle={t('bot.model.subtitle')}>
        <div className="space-y-4">
          <Field label={t('bot.model.provider')} help={t('bot.model.providerHelp')}>
            <select className="input" value={form.provider} onChange={(e) => set('provider', e.target.value)}>
              {PROVIDERS.map((p) => (
                <option key={p} value={p}>
                  {t(`bot.model.providers.${p}`)}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('bot.model.model')} help={t('bot.model.modelHelp')}>
            <input className="input text-left" dir="ltr" list="model-suggestions" value={form.model} onChange={(e) => set('model', e.target.value)} required />
            <datalist id="model-suggestions">
              {suggestions.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </Field>
          <RangeField
            label={t('bot.model.temperature')}
            help={t('bot.model.temperatureHelp')}
            value={form.temperature}
            min={0}
            max={1}
            step={0.05}
            onChange={(v) => set('temperature', v)}
          />
          {form.provider !== 'openai' && <InlineNotice>{t('bot.model.providerNotice')}</InlineNotice>}
        </div>
      </SectionCard>

      <SectionCard title={t('bot.model.retrievalTitle')} subtitle={t('bot.model.retrievalSubtitle')}>
        <div className="space-y-4">
          <Field label={t('bot.model.topKGeneral')} help={t('bot.model.topKGeneralHelp')}>
            <input type="number" min={1} max={30} className="input w-32" value={form.top_k_general} onChange={(e) => set('top_k_general', Number(e.target.value))} />
          </Field>
          <Field label={t('bot.model.topKQa')} help={t('bot.model.topKQaHelp')}>
            <input type="number" min={0} max={20} className="input w-32" value={form.top_k_qa} onChange={(e) => set('top_k_qa', Number(e.target.value))} />
          </Field>
          <RangeField
            label={t('bot.model.scoreThreshold')}
            help={t('bot.model.scoreThresholdHelp')}
            value={form.score_threshold}
            min={0}
            max={1}
            step={0.05}
            onChange={(v) => set('score_threshold', v)}
          />
        </div>
        <div className="mt-5 flex items-center justify-between gap-3">
          <InlineError message={save.isError ? t('form.saveFailed', { reason: errorDetail(save.error) }) : null} />
          <SaveButton saving={save.isPending} saved={saved} disabled={!dirty} />
        </div>
      </SectionCard>
    </form>
  )
}

export function RangeField({
  label,
  help,
  value,
  min,
  max,
  step,
  onChange,
  format = (v: number) => v.toFixed(2).replace(/\.?0+$/, ''),
}: {
  label: string
  help?: string
  value: number
  min: number
  max: number
  step: number
  onChange: (value: number) => void
  format?: (value: number) => string
}) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <span className="label mb-0">{label}</span>
        <span className="rounded-lg bg-brand-50 px-2 py-0.5 font-mono text-xs text-brand-800" dir="ltr">
          {format(value)}
        </span>
      </div>
      <input type="range" dir="ltr" className="w-full accent-brand-600" min={min} max={max} step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} />
      {help && <span className="mt-1 block text-xs text-ink-muted">{help}</span>}
    </div>
  )
}
