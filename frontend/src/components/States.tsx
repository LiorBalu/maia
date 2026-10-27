import { AlertTriangle, Inbox } from 'lucide-react'
import { useTranslation } from 'react-i18next'

export function ErrorState({ onRetry }: { onRetry?: () => void }) {
  const { t } = useTranslation()
  return (
    <div className="card flex flex-col items-center gap-3 px-6 py-12 text-center" role="alert">
      <div className="grid h-12 w-12 place-items-center rounded-2xl bg-red-50 text-red-600">
        <AlertTriangle size={24} />
      </div>
      <p className="text-sm text-ink-soft">{t('common.error')}</p>
      {onRetry && (
        <button type="button" className="btn-soft" onClick={onRetry}>
          {t('common.retry')}
        </button>
      )}
    </div>
  )
}

export function EmptyState({ text }: { text: string }) {
  return (
    <div className="card flex flex-col items-center gap-3 px-6 py-12 text-center">
      <div className="grid h-12 w-12 place-items-center rounded-2xl bg-brand-50 text-brand-600">
        <Inbox size={24} />
      </div>
      <p className="text-sm text-ink-soft">{text}</p>
    </div>
  )
}

export function SkeletonCards({ count = 6 }: { count?: number }) {
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3" aria-busy>
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="card h-36 animate-pulse p-5">
          <div className="h-4 w-1/2 rounded bg-brand-100" />
          <div className="mt-3 h-3 w-3/4 rounded bg-brand-50" />
          <div className="mt-2 h-3 w-2/3 rounded bg-brand-50" />
        </div>
      ))}
    </div>
  )
}
