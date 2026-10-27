import { useMemo, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Plus, Search, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate } from 'react-router-dom'

import { createTenant, toTenantSlug } from '@/api/admin'
import { listTenants, tenantChannels, type TenantSummary } from '@/api/tenants'
import { ChannelBadge, StatusBadge } from '@/components/Badges'
import { Field, InlineError, SaveButton } from '@/components/Form'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState, ErrorState, SkeletonCards } from '@/components/States'
import { errorDetail } from '@/lib/errors'

export function TenantsPage() {
  const { t } = useTranslation()
  const [query, setQuery] = useState('')
  const [creating, setCreating] = useState(false)
  const { data, isLoading, isError, refetch } = useQuery({ queryKey: ['tenants'], queryFn: listTenants })

  const tenants = useMemo(() => {
    const sorted = [...(data ?? [])].sort((a, b) => a.name.localeCompare(b.name))
    const needle = query.trim().toLowerCase()
    if (!needle) return sorted
    return sorted.filter(
      (s) => s.name.toLowerCase().includes(needle) || (s.description ?? '').toLowerCase().includes(needle),
    )
  }, [data, query])

  return (
    <>
      <PageHeader
        title={t('tenants.title')}
        subtitle={t('tenants.subtitle')}
        actions={
          <div className="flex items-center gap-2">
            {data && <span className="badge bg-brand-100 text-brand-800">{t('tenants.count', { count: data.length })}</span>}
            <button type="button" className="btn-primary" onClick={() => setCreating(true)}>
              <Plus size={16} />
              {t('tenants.create.button')}
            </button>
          </div>
        }
      />

      {creating && <CreateTenantForm onClose={() => setCreating(false)} />}

      <div className="relative mb-5 max-w-sm">
        <Search size={16} className="pointer-events-none absolute inset-y-0 start-3 my-auto text-ink-muted" />
        <input
          type="search"
          className="input ps-9"
          placeholder={t('tenants.search')}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {isLoading && <SkeletonCards />}
      {isError && <ErrorState onRetry={() => void refetch()} />}
      {data && tenants.length === 0 && <EmptyState text={t('tenants.empty')} />}
      {tenants.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {tenants.map((tenant) => (
            <TenantCard key={tenant.name} tenant={tenant} />
          ))}
        </div>
      )}
    </>
  )
}

function CreateTenantForm({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [display, setDisplay] = useState('')
  const [slug, setSlug] = useState('')
  const [slugTouched, setSlugTouched] = useState(false)
  const [language, setLanguage] = useState('he')
  const [timezone, setTimezone] = useState('Asia/Jerusalem')

  const effectiveSlug = slugTouched ? toTenantSlug(slug) : toTenantSlug(display)
  const create = useMutation({
    mutationFn: () => createTenant({ name: effectiveSlug, description: display.trim(), default_language: language, default_timezone: timezone }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['tenants'] })
      onClose()
      navigate(`/s/${encodeURIComponent(effectiveSlug)}/settings`)
    },
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    if (!effectiveSlug || !display.trim()) return
    create.mutate()
  }

  return (
    <form onSubmit={submit} className="card mb-5 space-y-3 p-5">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-base font-semibold text-ink">{t('tenants.create.title')}</div>
          <div className="text-xs text-ink-soft">{t('tenants.create.subtitle')}</div>
        </div>
        <button type="button" className="btn-ghost p-1.5" onClick={onClose} aria-label={t('common.close')}>
          <X size={16} />
        </button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t('tenants.create.display')} help={t('tenants.create.displayHelp')}>
          <input className="input" value={display} onChange={(e) => setDisplay(e.target.value)} dir="auto" required autoFocus />
        </Field>
        <Field label={t('tenants.create.slug')} help={t('tenants.create.slugHelp')}>
          <input
            className="input text-left font-mono"
            dir="ltr"
            value={slugTouched ? slug : effectiveSlug}
            onChange={(e) => {
              setSlugTouched(true)
              setSlug(e.target.value)
            }}
            placeholder="my-business"
            required
          />
        </Field>
        <Field label={t('common.language')}>
          <select className="input" value={language} onChange={(e) => setLanguage(e.target.value)}>
            {['he', 'en', 'ar', 'ru'].map((l) => (
              <option key={l} value={l}>
                {t(`settings.general.languages.${l}`)}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('common.timezone')}>
          <input className="input text-left" dir="ltr" value={timezone} onChange={(e) => setTimezone(e.target.value)} />
        </Field>
      </div>
      <div className="flex items-center justify-between gap-3">
        <InlineError message={create.isError ? t('tenants.create.failed', { reason: errorDetail(create.error) }) : null} />
        <div className="flex gap-2">
          <button type="button" className="btn-ghost" onClick={onClose}>
            {t('knowledge.qa.cancel')}
          </button>
          <SaveButton saving={create.isPending} disabled={!effectiveSlug || !display.trim()} label={t('tenants.create.submit')} />
        </div>
      </div>
    </form>
  )
}

function TenantCard({ tenant }: { tenant: TenantSummary }) {
  const { t } = useTranslation()
  const channels = tenantChannels(tenant)
  return (
    <Link
      to={`/s/${encodeURIComponent(tenant.name)}/inbox`}
      className="card group flex flex-col gap-3 p-5 transition hover:-translate-y-0.5 hover:shadow-pop"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-base font-semibold text-ink" dir="auto">
            {tenant.description || tenant.name}
          </div>
          <div className="mt-0.5 truncate text-xs text-ink-muted" dir="ltr">
            {tenant.name}
          </div>
        </div>
        <StatusBadge status={tenant.status} />
      </div>
      <div className="flex flex-wrap gap-1.5">
        {channels.length > 0 ? (
          channels.map((channel) => <ChannelBadge key={channel} channel={channel} />)
        ) : (
          <span className="text-xs text-ink-muted">{t('tenants.noChannels')}</span>
        )}
      </div>
      <div className="mt-auto flex items-center justify-between pt-1 text-xs text-ink-muted">
        <span dir="ltr">{[tenant.default_language?.toUpperCase(), tenant.default_timezone].filter(Boolean).join(' · ')}</span>
        <span className="btn-soft px-3 py-1 group-hover:bg-brand-200">
          {t('common.enter')}
          <ArrowLeft size={14} />
        </span>
      </div>
    </Link>
  )
}
