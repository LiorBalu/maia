import clsx from 'clsx'
import { Bot, UserRound } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { ReplyMode } from '@/api/conversations'

/** Who answers the customer: the bot or a human. "blocked" is shown but not offered. */
export function ReplyModeSwitch({
  value,
  onChange,
  saving,
  compact = false,
}: {
  value: ReplyMode
  onChange: (mode: ReplyMode) => void
  saving: boolean
  compact?: boolean
}) {
  const { t } = useTranslation()
  const options: { mode: ReplyMode; icon: typeof Bot }[] = [
    { mode: 'AI', icon: Bot },
    { mode: 'agent', icon: UserRound },
  ]
  return (
    <div
      className={clsx('inline-flex rounded-xl bg-brand-50 p-0.5 ring-1 ring-brand-100', saving && 'opacity-60')}
      role="radiogroup"
      aria-label={t('inbox.replyMode.label')}
    >
      {options.map(({ mode, icon: Icon }) => {
        const active = value === mode
        return (
          <button
            key={mode}
            type="button"
            role="radio"
            aria-checked={active}
            disabled={saving}
            onClick={() => !active && onChange(mode)}
            className={clsx(
              'flex items-center gap-1.5 rounded-[10px] px-3 py-1.5 text-xs font-medium transition-colors',
              active ? 'bg-white text-brand-800 shadow-sm' : 'text-ink-soft hover:text-ink',
            )}
          >
            <Icon size={14} />
            <span className={clsx(compact && 'hidden md:inline')}>{t(`inbox.replyMode.${mode}`)}</span>
          </button>
        )
      })}
      {value === 'blocked' && <span className="badge bg-red-50 text-red-700">{t('inbox.replyMode.blocked')}</span>}
    </div>
  )
}
