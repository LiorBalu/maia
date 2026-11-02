import { useMemo, useState, type FormEvent } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { ListPlus, Pencil, Plus, Trash2, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import {
  QA_PAGE_SIZE,
  createQA,
  createQABulk,
  deleteQA,
  getQACategories,
  getQACount,
  listQA,
  updateQA,
  type QAInput,
  type QAItem,
} from '@/api/knowledge'
import { EmptyState, ErrorState } from '@/components/States'
import { errorDetail } from '@/lib/errors'

export function QATab({ tenant }: { tenant: string }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [category, setCategory] = useState<string | undefined>()
  const [adding, setAdding] = useState(false)
  const [bulkOpen, setBulkOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const count = useQuery({ queryKey: ['qa-count', tenant], queryFn: () => getQACount(tenant) })
  const categories = useQuery({ queryKey: ['qa-categories', tenant], queryFn: () => getQACategories(tenant) })
  const list = useInfiniteQuery({
    queryKey: ['qa', tenant, category] as const,
    queryFn: ({ pageParam }) => listQA(tenant, pageParam, category),
    initialPageParam: 1,
    getNextPageParam: (last, pages) => (last.length === QA_PAGE_SIZE ? pages.length + 1 : undefined),
  })
  const items = useMemo(() => list.data?.pages.flat() ?? [], [list.data])
  const categoryNames = (categories.data ?? []).map((c) => c.category).filter((c) => c && c !== 'general')

  const invalidateAll = () => {
    void queryClient.invalidateQueries({ queryKey: ['qa', tenant] })
    void queryClient.invalidateQueries({ queryKey: ['qa-count', tenant] })
    void queryClient.invalidateQueries({ queryKey: ['qa-categories', tenant] })
  }

  const create = useMutation({
    mutationFn: (input: QAInput) => createQA(tenant, input),
    onMutate: () => setError(null),
    onSuccess: () => {
      setAdding(false)
      invalidateAll()
    },
    onError: (e) => setError(t('knowledge.qa.saveFailed', { reason: errorDetail(e) })),
  })
  const bulk = useMutation({
    mutationFn: (inputs: QAInput[]) => createQABulk(tenant, inputs),
    onMutate: () => setError(null),
    onSuccess: () => {
      setBulkOpen(false)
      invalidateAll()
    },
    onError: (e) => setError(t('knowledge.qa.saveFailed', { reason: errorDetail(e) })),
  })
  const update = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: Partial<QAInput> }) => updateQA(id, patch),
    onMutate: () => setError(null),
    onSuccess: invalidateAll,
    onError: (e) => setError(t('knowledge.qa.saveFailed', { reason: errorDetail(e) })),
  })
  const remove = useMutation({
    mutationFn: (id: string) => deleteQA(id),
    onSuccess: invalidateAll,
    onError: (e) => setError(t('knowledge.qa.deleteFailed', { reason: errorDetail(e) })),
  })

  return (
    <div className="space-y-4">
      <div className="card flex flex-wrap items-center gap-3 p-4">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold text-ink">{t('knowledge.qa.title')}</div>
          <div className="text-xs text-ink-soft">{t('knowledge.qa.hint')}</div>
          {count.data && (
            <div className="mt-1 flex flex-wrap gap-2 text-xs">
              <span className="badge bg-brand-100 text-brand-800">{t('knowledge.qa.count', { count: count.data.total_count })}</span>
              {count.data.pending_count > 0 && (
                <span className="badge bg-amber-50 text-amber-700">{t('knowledge.qa.pending', { count: count.data.pending_count })}</span>
              )}
            </div>
          )}
        </div>
        <button type="button" className="btn-primary" onClick={() => { setAdding(true); setBulkOpen(false) }}>
          <Plus size={16} />
          {t('knowledge.qa.add')}
        </button>
        <button type="button" className={clsx('btn-soft', bulkOpen && 'ring-2 ring-brand-300')} onClick={() => { setBulkOpen((v) => !v); setAdding(false) }}>
          <ListPlus size={16} />
          {t('knowledge.qa.bulk')}
        </button>
      </div>

      {error && (
        <div role="alert" className="rounded-xl bg-red-50 px-4 py-2 text-sm text-red-700 ring-1 ring-red-200">
          {error}
        </div>
      )}

      {adding && (
        <QAForm
          categories={categoryNames}
          saving={create.isPending}
          onCancel={() => setAdding(false)}
          onSubmit={(input) => create.mutate(input)}
        />
      )}
      {bulkOpen && <BulkForm saving={bulk.isPending} onCancel={() => setBulkOpen(false)} onSubmit={(inputs) => bulk.mutate(inputs)} />}

      {categoryNames.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          <Chip active={!category} onClick={() => setCategory(undefined)}>
            {t('knowledge.qa.allCategories')}
          </Chip>
          {categoryNames.map((name) => (
            <Chip key={name} active={category === name} onClick={() => setCategory(name)}>
              {name}
            </Chip>
          ))}
        </div>
      )}

      {list.isLoading && <div className="card p-6 text-sm text-ink-muted">{t('common.loading')}</div>}
      {list.isError && <ErrorState onRetry={() => void list.refetch()} />}
      {list.data && items.length === 0 && <EmptyState text={category ? t('knowledge.qa.emptyFiltered') : t('knowledge.qa.empty')} />}
      {items.length > 0 && (
        <ul className="card divide-y divide-brand-50">
          {items.map((item) => (
            <QARow
              key={item.id}
              item={item}
              categories={categoryNames}
              saving={update.isPending && update.variables?.id === item.id}
              onSave={(patch) => update.mutate({ id: item.id, patch })}
              onDelete={() => {
                if (window.confirm(t('knowledge.qa.deleteConfirm'))) remove.mutate(item.id)
              }}
            />
          ))}
        </ul>
      )}
      {list.hasNextPage && (
        <div className="text-center">
          <button type="button" className="btn-soft" onClick={() => void list.fetchNextPage()} disabled={list.isFetchingNextPage}>
            {list.isFetchingNextPage ? t('common.loading') : t('knowledge.qa.loadMore')}
          </button>
        </div>
      )}
    </div>
  )
}

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        'rounded-full px-2.5 py-1 text-xs font-medium transition-colors',
        active ? 'bg-brand-600 text-white' : 'bg-white text-ink-soft ring-1 ring-brand-100 hover:bg-brand-50 hover:text-brand-800',
      )}
    >
      {children}
    </button>
  )
}

function QAForm({
  initial,
  categories,
  saving,
  onCancel,
  onSubmit,
}: {
  initial?: QAItem
  categories: string[]
  saving: boolean
  onCancel: () => void
  onSubmit: (input: QAInput) => void
}) {
  const { t } = useTranslation()
  const [question, setQuestion] = useState(initial?.question ?? '')
  const [answer, setAnswer] = useState(initial?.answer ?? '')
  const [category, setCategory] = useState(initial?.category && initial.category !== 'general' ? initial.category : '')

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!question.trim() || !answer.trim()) return
    onSubmit({ question: question.trim(), answer: answer.trim(), category: category.trim() || undefined })
  }

  return (
    <form onSubmit={submit} className="card space-y-3 p-4">
      <div className="flex items-center justify-between">
        <div className="text-sm font-semibold text-ink">{initial ? t('knowledge.qa.editTitle') : t('knowledge.qa.addTitle')}</div>
        <button type="button" className="btn-ghost p-1.5" onClick={onCancel} aria-label={t('common.close')}>
          <X size={16} />
        </button>
      </div>
      <div>
        <label className="label">{t('knowledge.qa.question')}</label>
        <input className="input" value={question} onChange={(e) => setQuestion(e.target.value)} dir="auto" required autoFocus />
      </div>
      <div>
        <label className="label">{t('knowledge.qa.answer')}</label>
        <textarea className="input min-h-[6rem]" value={answer} onChange={(e) => setAnswer(e.target.value)} dir="auto" required />
      </div>
      <div className="max-w-xs">
        <label className="label">{t('knowledge.qa.category')}</label>
        <input className="input" list="qa-categories" value={category} onChange={(e) => setCategory(e.target.value)} dir="auto" placeholder={t('knowledge.qa.categoryPlaceholder')} />
        <datalist id="qa-categories">
          {categories.map((c) => (
            <option key={c} value={c} />
          ))}
        </datalist>
      </div>
      <div className="flex gap-2">
        <button type="submit" className="btn-primary" disabled={saving || !question.trim() || !answer.trim()}>
          {saving ? t('knowledge.qa.saving') : t('knowledge.qa.save')}
        </button>
        <button type="button" className="btn-ghost" onClick={onCancel}>
          {t('knowledge.qa.cancel')}
        </button>
      </div>
    </form>
  )
}

/** Paste many pairs at once: one per line, "question | answer | category" (category optional). */
function BulkForm({ saving, onCancel, onSubmit }: { saving: boolean; onCancel: () => void; onSubmit: (inputs: QAInput[]) => void }) {
  const { t } = useTranslation()
  const [text, setText] = useState('')
  const parsed = useMemo(
    () =>
      text
        .split('\n')
        .map((line) => line.split('|').map((s) => s.trim()))
        .filter((cols) => cols.length >= 2 && cols[0] && cols[1])
        .map((cols) => ({ question: cols[0], answer: cols[1], category: cols[2] || undefined })),
    [text],
  )
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        if (parsed.length) onSubmit(parsed)
      }}
      className="card space-y-3 p-4"
    >
      <div className="flex items-center justify-between">
        <div className="text-sm font-semibold text-ink">{t('knowledge.qa.bulkTitle')}</div>
        <button type="button" className="btn-ghost p-1.5" onClick={onCancel} aria-label={t('common.close')}>
          <X size={16} />
        </button>
      </div>
      <p className="text-xs text-ink-soft">{t('knowledge.qa.bulkHint')}</p>
      <textarea className="input min-h-[8rem] font-mono text-xs" value={text} onChange={(e) => setText(e.target.value)} dir="auto" placeholder={t('knowledge.qa.bulkPlaceholder')} />
      <div className="flex items-center gap-3">
        <button type="submit" className="btn-primary" disabled={saving || parsed.length === 0}>
          {saving ? t('knowledge.qa.saving') : t('knowledge.qa.bulkSubmit', { count: parsed.length })}
        </button>
        <button type="button" className="btn-ghost" onClick={onCancel}>
          {t('knowledge.qa.cancel')}
        </button>
      </div>
    </form>
  )
}

function QARow({
  item,
  categories,
  saving,
  onSave,
  onDelete,
}: {
  item: QAItem
  categories: string[]
  saving: boolean
  onSave: (patch: Partial<QAInput>) => void
  onDelete: () => void
}) {
  const { t } = useTranslation()
  const [editing, setEditing] = useState(false)
  const [expanded, setExpanded] = useState(false)

  if (editing) {
    return (
      <li className="p-3">
        <QAForm
          initial={item}
          categories={categories}
          saving={saving}
          onCancel={() => setEditing(false)}
          onSubmit={(input) => {
            onSave(input)
            setEditing(false)
          }}
        />
      </li>
    )
  }

  const long = item.answer.length > 220
  return (
    <li className="flex gap-3 px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="font-medium text-ink" dir="auto">
          {item.question}
        </div>
        <p className={clsx('mt-1 whitespace-pre-wrap text-sm text-ink-soft', !expanded && 'line-clamp-3')} dir="auto">
          {item.answer}
        </p>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-xs">
          {long && (
            <button type="button" className="text-brand-700 hover:underline" onClick={() => setExpanded((v) => !v)}>
              {expanded ? t('knowledge.qa.less') : t('knowledge.qa.more')}
            </button>
          )}
          {item.category && item.category !== 'general' && <span className="badge bg-brand-50 text-brand-800 ring-1 ring-brand-100">{item.category}</span>}
          {!item.is_indexed && <span className="badge bg-amber-50 text-amber-700">{t('knowledge.qa.notIndexed')}</span>}
        </div>
      </div>
      <div className="flex shrink-0 items-start gap-1">
        <button type="button" className="btn-ghost p-2" onClick={() => setEditing(true)} title={t('knowledge.qa.edit')} aria-label={t('knowledge.qa.edit')}>
          <Pencil size={15} />
        </button>
        <button type="button" className="btn-ghost p-2 text-red-600 hover:bg-red-50 hover:text-red-700" onClick={onDelete} title={t('knowledge.qa.delete')} aria-label={t('knowledge.qa.delete')}>
          <Trash2 size={15} />
        </button>
      </div>
    </li>
  )
}
