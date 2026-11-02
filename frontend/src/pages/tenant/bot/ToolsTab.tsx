import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { getTenantDetail, updateTenant } from '@/api/settings'
import { listTools, type ToolInfo } from '@/api/tools'
import { InlineError, InlineNotice, SaveButton, SectionCard, Toggle, useSavedFlash } from '@/components/Form'
import { EmptyState, ErrorState } from '@/components/States'
import { errorDetail } from '@/lib/errors'

/** Groups shown as cards, in this order; any other module falls into `other`. */
const GROUPS = ['google_calendar', 'google_sheets', 'other'] as const
type Group = (typeof GROUPS)[number]

function groupOf(tool: ToolInfo): Group {
  return tool.module === 'google_calendar' || tool.module === 'google_sheets' ? tool.module : 'other'
}

/**
 * Bot > Tools: the registered `@gpt_tool` functions as switches. The chosen names are saved in the
 * tenant's `tools` field and bound to every turn (chat and phone) by the backend; a tool that is off
 * does not exist for the bot at all.
 */
export function ToolsTab({ tenant }: { tenant: string }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const tools = useQuery({ queryKey: ['tools'], queryFn: listTools })
  const detail = useQuery({ queryKey: ['tenant-detail', tenant], queryFn: () => getTenantDetail(tenant) })

  const savedTools = detail.data?.tools ?? []
  const [selected, setSelected] = useState<string[]>(savedTools)
  useEffect(() => setSelected(savedTools), [savedTools.join(',')]) // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = [...selected].sort().join(',') !== [...savedTools].sort().join(',')

  const save = useMutation({
    mutationFn: () => updateTenant(tenant, { tools: selected }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['tenant-detail', tenant] }),
  })
  const saved = useSavedFlash(save.isSuccess ? save.submittedAt : 0)

  if (tools.isLoading || detail.isLoading) return <div className="card p-6 text-sm text-ink-muted">{t('common.loading')}</div>
  if (tools.isError || !tools.data) return <ErrorState onRetry={() => void tools.refetch()} />
  if (detail.isError || !detail.data) return <ErrorState onRetry={() => void detail.refetch()} />
  if (tools.data.length === 0) return <EmptyState text={t('bot.tools.empty')} />

  const toggle = (name: string, on: boolean) => setSelected((s) => (on ? [...new Set([...s, name])] : s.filter((n) => n !== name)))
  const setGroup = (names: string[], on: boolean) =>
    setSelected((s) => (on ? [...new Set([...s, ...names])] : s.filter((n) => !names.includes(n))))

  const groups = GROUPS.map((group) => ({ group, items: tools.data!.filter((tool) => groupOf(tool) === group) })).filter((g) => g.items.length > 0)

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        if (dirty) save.mutate()
      }}
      className="space-y-4"
    >
      <InlineNotice>{t('bot.tools.notice')}</InlineNotice>

      <div className="grid gap-4 xl:grid-cols-2">
        {groups.map(({ group, items }) => {
          const names = items.map((tool) => tool.name)
          const allOn = names.every((n) => selected.includes(n))
          return (
            <SectionCard
              key={group}
              title={t(`bot.tools.groups.${group}`)}
              subtitle={t(`bot.tools.groupHelp.${group}`)}
              actions={
                <button type="button" className="btn-ghost text-xs" onClick={() => setGroup(names, !allOn)}>
                  {allOn ? t('bot.tools.none') : t('bot.tools.all')}
                </button>
              }
            >
              <div className="divide-y divide-slate-100">
                {items.map((tool) => (
                  <Toggle
                    key={tool.name}
                    checked={selected.includes(tool.name)}
                    onChange={(on) => toggle(tool.name, on)}
                    label={t(`bot.tools.names.${tool.name}`, { defaultValue: tool.name })}
                    help={t(`bot.tools.descriptions.${tool.name}`, { defaultValue: tool.description })}
                  />
                ))}
              </div>
              {group === 'google_calendar' && (
                <Link to={`/s/${tenant}/calendar`} className="mt-3 inline-block text-xs text-brand-700 hover:underline">
                  {t('bot.tools.openCalendar')}
                </Link>
              )}
              {group === 'google_sheets' && (
                <Link to={`/s/${tenant}/settings?tab=sheets`} className="mt-3 inline-block text-xs text-brand-700 hover:underline">
                  {t('bot.tools.openSheets')}
                </Link>
              )}
            </SectionCard>
          )
        })}
      </div>

      <div className="card flex items-center justify-between gap-3 p-4">
        <div className="text-sm text-ink-muted">
          {t('bot.tools.count', { count: selected.length })}
          <InlineError message={save.isError ? t('form.saveFailed', { reason: errorDetail(save.error) }) : null} />
        </div>
        <SaveButton saving={save.isPending} saved={saved} disabled={!dirty} />
      </div>
    </form>
  )
}
