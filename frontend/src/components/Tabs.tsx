import clsx from 'clsx'
import type { LucideIcon } from 'lucide-react'

export interface TabItem<K extends string> {
  key: K
  label: string
  icon?: LucideIcon
  badge?: string | number
}

/** Segmented tabs used by the knowledge, bot and settings screens. */
export function Tabs<K extends string>({ tabs, value, onChange }: { tabs: TabItem<K>[]; value: K; onChange: (key: K) => void }) {
  return (
    <div className="mb-5 inline-flex max-w-full gap-1 overflow-x-auto rounded-2xl bg-white p-1 shadow-card ring-1 ring-brand-100" role="tablist">
      {tabs.map((tab) => {
        const active = tab.key === value
        return (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(tab.key)}
            className={clsx(
              'flex shrink-0 items-center gap-2 rounded-xl px-4 py-2 text-sm font-medium transition-colors',
              active ? 'bg-brand-600 text-white shadow-sm' : 'text-ink-soft hover:bg-brand-50 hover:text-brand-800',
            )}
          >
            {tab.icon && <tab.icon size={16} />}
            {tab.label}
            {tab.badge !== undefined && (
              <span className={clsx('rounded-full px-1.5 text-[11px]', active ? 'bg-white/20' : 'bg-brand-100 text-brand-800')}>
                {tab.badge}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}
