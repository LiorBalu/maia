import { useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { ChevronDown, ChevronUp, ExternalLink, FileSpreadsheet, FileText, Globe, Link2, Trash2, Upload } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import {
  DOCUMENT_ACCEPT,
  addUrlDocument,
  deleteDocument,
  getDocumentProgress,
  listDocumentParts,
  listDocuments,
  uploadDocument,
  type KnowledgeDocument,
} from '@/api/knowledge'
import { EmptyState, ErrorState } from '@/components/States'
import { formatDateTime, parseApiDate } from '@/lib/dates'
import { errorDetail } from '@/lib/errors'

export function DocumentsTab({ tenant }: { tenant: string }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const fileInput = useRef<HTMLInputElement | null>(null)
  const [urlOpen, setUrlOpen] = useState(false)
  const [url, setUrl] = useState('')
  const [error, setError] = useState<string | null>(null)

  const docs = useQuery({ queryKey: ['documents', tenant], queryFn: () => listDocuments(tenant), enabled: Boolean(tenant) })
  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['documents', tenant] })

  const upload = useMutation({
    mutationFn: (file: File) => uploadDocument(tenant, file),
    onMutate: () => setError(null),
    onSuccess: invalidate,
    onError: (e) => setError(t('knowledge.docs.uploadFailed', { reason: errorDetail(e) })),
  })
  const addUrl = useMutation({
    mutationFn: (value: string) => addUrlDocument(tenant, value, 1),
    onMutate: () => setError(null),
    onSuccess: () => {
      setUrl('')
      setUrlOpen(false)
      invalidate()
    },
    onError: (e) => setError(t('knowledge.docs.urlFailed', { reason: errorDetail(e) })),
  })
  const remove = useMutation({
    mutationFn: (id: string) => deleteDocument(id),
    onSuccess: invalidate,
    onError: (e) => setError(t('knowledge.docs.deleteFailed', { reason: errorDetail(e) })),
  })

  function onFilesChosen(files: FileList | null) {
    if (!files) return
    for (const file of Array.from(files)) upload.mutate(file)
    if (fileInput.current) fileInput.current.value = ''
  }

  function submitUrl(event: FormEvent) {
    event.preventDefault()
    const value = url.trim()
    if (!value) return
    addUrl.mutate(/^https?:\/\//i.test(value) ? value : `https://${value}`)
  }

  const sorted = [...(docs.data ?? [])].sort(
    (a, b) => (parseApiDate(b.created_at)?.getTime() ?? 0) - (parseApiDate(a.created_at)?.getTime() ?? 0),
  )

  return (
    <div className="space-y-4">
      <div className="card flex flex-wrap items-center gap-3 p-4">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold text-ink">{t('knowledge.docs.addTitle')}</div>
          <div className="text-xs text-ink-soft">{t('knowledge.docs.addHint')}</div>
        </div>
        <input ref={fileInput} type="file" accept={DOCUMENT_ACCEPT} multiple className="hidden" onChange={(e) => onFilesChosen(e.target.files)} />
        <button type="button" className="btn-primary" onClick={() => fileInput.current?.click()} disabled={upload.isPending}>
          <Upload size={16} />
          {upload.isPending ? t('knowledge.docs.uploading') : t('knowledge.docs.upload')}
        </button>
        <button type="button" className={clsx('btn-soft', urlOpen && 'ring-2 ring-brand-300')} onClick={() => setUrlOpen((v) => !v)}>
          <Link2 size={16} />
          {t('knowledge.docs.addUrl')}
        </button>
        {urlOpen && (
          <form onSubmit={submitUrl} className="flex w-full flex-wrap items-center gap-2 border-t border-brand-100 pt-3">
            <input
              type="url"
              dir="ltr"
              className="input min-w-0 flex-1 text-left"
              placeholder="https://example.co.il/faq"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              required
            />
            <button type="submit" className="btn-primary" disabled={addUrl.isPending || !url.trim()}>
              {addUrl.isPending ? t('knowledge.docs.urlAdding') : t('knowledge.docs.urlSubmit')}
            </button>
            <span className="w-full text-xs text-ink-muted">{t('knowledge.docs.urlHint')}</span>
          </form>
        )}
      </div>

      {error && (
        <div role="alert" className="rounded-xl bg-red-50 px-4 py-2 text-sm text-red-700 ring-1 ring-red-200">
          {error}
        </div>
      )}

      {docs.isLoading && <DocsSkeleton />}
      {docs.isError && <ErrorState onRetry={() => void docs.refetch()} />}
      {docs.data && sorted.length === 0 && <EmptyState text={t('knowledge.docs.empty')} />}
      {sorted.length > 0 && (
        <ul className="space-y-3">
          {sorted.map((doc) => (
            <DocumentRow key={doc.id} doc={doc} tenant={tenant} onDelete={() => remove.mutate(doc.id)} deleting={remove.isPending && remove.variables === doc.id} />
          ))}
        </ul>
      )}
    </div>
  )
}

function DocumentRow({ doc, tenant, onDelete, deleting }: { doc: KnowledgeDocument; tenant: string; onDelete: () => void; deleting: boolean }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const created = parseApiDate(doc.created_at)
  const isUrl = /^https?:\/\//i.test(doc.title) || (!/\.[a-z0-9]{2,5}$/i.test(doc.title) && doc.url && !doc.url.includes('/embedded_documents/'))
  const Icon = isUrl ? Globe : /\.(xlsx?|csv)$/i.test(doc.title) ? FileSpreadsheet : FileText

  // Poll the indexing progress until the document is indexed, then refresh the list.
  const progress = useQuery({
    queryKey: ['document-progress', doc.id],
    queryFn: () => getDocumentProgress(doc.id),
    enabled: !doc.is_indexed,
    refetchInterval: (query) => (query.state.data?.is_indexed || query.state.data?.status === 'failed' ? false : 3000),
  })
  if (!doc.is_indexed && progress.data?.is_indexed) void queryClient.invalidateQueries({ queryKey: ['documents', tenant] })

  const failed = progress.data?.status === 'failed' || progress.data?.status === 'error'
  const percent = progress.data?.progress_percentage ?? 0

  const parts = useQuery({
    queryKey: ['document-parts', doc.id],
    queryFn: () => listDocumentParts(doc.id, 1, 20),
    enabled: open,
  })

  return (
    <li className="card p-4">
      <div className="flex flex-wrap items-start gap-3">
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-brand-100 text-brand-700">
          <Icon size={20} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate font-medium text-ink" dir="auto">
              {doc.title}
            </span>
            {doc.is_indexed ? (
              <span className="badge bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200">{t('knowledge.docs.indexed')}</span>
            ) : failed ? (
              <span className="badge bg-red-50 text-red-700 ring-1 ring-red-200">{t('knowledge.docs.failed')}</span>
            ) : (
              <span className="badge bg-amber-50 text-amber-700 ring-1 ring-amber-200">{t('knowledge.docs.processing', { percent: Math.round(percent) })}</span>
            )}
          </div>
          <div className="mt-0.5 flex flex-wrap gap-x-3 text-xs text-ink-soft">
            {created && <span>{formatDateTime(created)}</span>}
            <span>{t('knowledge.docs.parts', { count: doc.total_parts })}</span>
          </div>
          {!doc.is_indexed && !failed && (
            <div className="mt-2 h-1.5 w-full max-w-xs overflow-hidden rounded-full bg-brand-100">
              <div className="h-full rounded-full bg-brand-500 transition-all" style={{ width: `${Math.max(4, percent)}%` }} />
            </div>
          )}
        </div>
        <div className="flex items-center gap-1">
          {doc.url && (
            <a href={doc.url} target="_blank" rel="noreferrer" className="btn-ghost p-2" title={t('knowledge.docs.open')} aria-label={t('knowledge.docs.open')}>
              <ExternalLink size={16} />
            </a>
          )}
          <button type="button" className="btn-ghost p-2" onClick={() => setOpen((v) => !v)} title={t('knowledge.docs.showParts')} aria-label={t('knowledge.docs.showParts')}>
            {open ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
          </button>
          <button
            type="button"
            className="btn-ghost p-2 text-red-600 hover:bg-red-50 hover:text-red-700"
            disabled={deleting}
            onClick={() => {
              if (window.confirm(t('knowledge.docs.deleteConfirm', { title: doc.title }))) onDelete()
            }}
            title={t('knowledge.docs.delete')}
            aria-label={t('knowledge.docs.delete')}
          >
            <Trash2 size={16} />
          </button>
        </div>
      </div>
      {open && (
        <div className="mt-3 border-t border-brand-100 pt-3">
          <div className="mb-2 text-xs font-semibold text-ink-muted">{t('knowledge.docs.partsTitle')}</div>
          {parts.isLoading && <div className="text-xs text-ink-muted">{t('common.loading')}</div>}
          {parts.data && parts.data.length === 0 && <div className="text-xs text-ink-muted">{t('knowledge.docs.noParts')}</div>}
          <ol className="space-y-2">
            {(parts.data ?? []).map((part) => (
              <li key={part.id} className="rounded-xl bg-surface px-3 py-2 text-xs text-ink-soft">
                <span className="me-2 font-semibold text-brand-700">#{Number(part.part_number) + 1}</span>
                <span className="whitespace-pre-wrap" dir="auto">
                  {part.content.length > 600 ? `${part.content.slice(0, 600)}…` : part.content}
                </span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </li>
  )
}

function DocsSkeleton() {
  return (
    <ul className="space-y-3" aria-busy>
      {[0, 1, 2].map((i) => (
        <li key={i} className="card flex items-center gap-3 p-4">
          <div className="h-10 w-10 animate-pulse rounded-xl bg-brand-100" />
          <div className="flex-1">
            <div className="h-3 w-1/3 animate-pulse rounded bg-brand-100" />
            <div className="mt-2 h-2.5 w-1/4 animate-pulse rounded bg-brand-50" />
          </div>
        </li>
      ))}
    </ul>
  )
}
