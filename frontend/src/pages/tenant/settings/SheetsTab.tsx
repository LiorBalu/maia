import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, RefreshCw, Save, Unplug } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { connectSheet, disconnectSheet, getSheet } from '@/api/sheets'
import { CopyField, Field, InlineError, InlineNotice, SectionCard } from '@/components/Form'
import { ErrorState } from '@/components/States'
import { errorDetail } from '@/lib/errors'

export function SheetsTab({ tenant }: { tenant: string }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [url, setUrl] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const sheet = useQuery({ queryKey: ['sheet', tenant], queryFn: () => getSheet(tenant) })
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ['sheet', tenant] })

  const connect = useMutation({
    mutationFn: () => connectSheet(tenant, url.trim()),
    onSuccess: () => {
      setError(null)
      setNotice(t('settings.sheets.connected'))
      setUrl('')
      refresh()
    },
    onError: (e) => setError(errorDetail(e)),
  })
  const disconnect = useMutation({
    mutationFn: () => disconnectSheet(tenant),
    onSuccess: () => {
      setError(null)
      setNotice(t('settings.sheets.disconnected'))
      refresh()
    },
    onError: (e) => setError(errorDetail(e)),
  })

  const data = sheet.data
  const preview = data?.preview ?? null

  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <SectionCard title={t('settings.sheets.title')} subtitle={t('settings.sheets.subtitle')}>
        <div className="space-y-3">
          <InlineError message={error} />
          {notice && <InlineNotice>{notice}</InlineNotice>}
          {sheet.isLoading && <div className="text-sm text-ink-muted">{t('common.loading')}</div>}
          {sheet.isError && <ErrorState onRetry={() => void sheet.refetch()} />}
          {data && <CopyField label={t('settings.sheets.shareWith')} value={data.service_account} help={t('settings.sheets.shareHelp')} />}
          <form
            className="space-y-2"
            onSubmit={(e) => {
              e.preventDefault()
              if (url.trim()) connect.mutate()
            }}
          >
            <Field label={t('settings.sheets.urlLabel')} help={t('settings.sheets.urlHelp')}>
              <input
                className="input text-left"
                dir="ltr"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://docs.google.com/spreadsheets/d/..."
              />
            </Field>
            <button type="submit" className="btn-primary" disabled={!url.trim() || connect.isPending}>
              <Save size={16} />
              {connect.isPending ? t('settings.sheets.saving') : t('settings.sheets.save')}
            </button>
          </form>
          {data && !data.sheet_id && <InlineNotice>{t('settings.sheets.none')}</InlineNotice>}
          {data?.sheet_id && (
            <div className="flex flex-wrap items-center gap-2 rounded-xl bg-surface p-3 text-sm">
              <span className="font-medium text-ink" dir="auto">
                {preview?.title ?? data.sheet_id}
              </span>
              {data.sheet_url && (
                <a className="btn-ghost text-xs" href={data.sheet_url} target="_blank" rel="noreferrer">
                  <ExternalLink size={14} />
                  {t('settings.sheets.open')}
                </a>
              )}
              <button
                type="button"
                className="btn-ghost ms-auto text-xs text-red-600 hover:bg-red-50"
                disabled={disconnect.isPending}
                onClick={() => {
                  if (window.confirm(t('settings.sheets.disconnectConfirm'))) disconnect.mutate()
                }}
              >
                <Unplug size={14} />
                {t('settings.sheets.disconnect')}
              </button>
            </div>
          )}
        </div>
      </SectionCard>

      <SectionCard
        title={t('settings.sheets.previewTitle')}
        subtitle={t('settings.sheets.previewSubtitle')}
        actions={
          <button type="button" className="btn-soft" onClick={() => void sheet.refetch()} disabled={sheet.isFetching || !data?.sheet_id}>
            <RefreshCw size={14} className={sheet.isFetching ? 'animate-spin' : undefined} />
            {t('settings.sheets.refresh')}
          </button>
        }
      >
        {data?.error && <InlineError message={data.error} />}
        {data && !data.sheet_id && <p className="text-sm text-ink-muted">{t('settings.sheets.previewHint')}</p>}
        {preview && (
          <>
            <p className="mb-2 text-xs text-ink-soft">{t('settings.sheets.rowCount', { count: preview.row_count })}</p>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr>
                    {preview.headers.map((h) => (
                      <th key={h} className="px-2 py-1 text-start font-medium text-ink-soft">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preview.rows.map((row, i) => (
                    <tr key={i} className="border-t border-brand-50">
                      {preview.headers.map((h) => (
                        <td key={h} className="px-2 py-1" dir="auto">
                          {row[h] ?? ''}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </SectionCard>
    </div>
  )
}
