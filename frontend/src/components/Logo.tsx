import clsx from 'clsx'
import { useTranslation } from 'react-i18next'

export function Logo({ size = 'md' }: { size?: 'md' | 'lg' }) {
  const { t } = useTranslation()
  const large = size === 'lg'
  return (
    <div className={clsx('flex items-center gap-3', large && 'flex-col text-center')}>
      <div
        className={clsx(
          'grid place-items-center rounded-2xl bg-gradient-to-br from-brand-400 to-brand-700 text-white shadow-card',
          large ? 'h-16 w-16' : 'h-9 w-9 rounded-xl',
        )}
        aria-hidden
      >
        <svg viewBox="0 0 64 64" className={large ? 'h-9 w-9' : 'h-5 w-5'} fill="none">
          <path
            d="M16 46V20l16 16 16-16v26"
            stroke="currentColor"
            strokeWidth="7"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
      <div>
        <div className={clsx('font-bold leading-tight text-ink', large ? 'text-2xl' : 'text-base')}>{t('app.name')}</div>
        <div className={clsx('text-ink-muted', large ? 'text-sm' : 'text-xs')}>{t('app.tagline')}</div>
      </div>
    </div>
  )
}
