import { useEffect, useState, type ReactNode } from 'react'
import clsx from 'clsx'
import { Check, Copy } from 'lucide-react'
import { useTranslation } from 'react-i18next'

/** Label + control + optional help line. */
export function Field({ label, help, children, className }: { label: string; help?: string; children: ReactNode; className?: string }) {
  return (
    <label className={clsx('block', className)}>
      <span className="label">{label}</span>
      {children}
      {help && <span className="mt-1 block text-xs text-ink-muted">{help}</span>}
    </label>
  )
}

/** On/off switch with a title and an explanation. */
export function Toggle({
  checked,
  onChange,
  label,
  help,
  disabled,
}: {
  checked: boolean
  onChange: (value: boolean) => void
  label: string
  help?: string
  disabled?: boolean
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-2">
      <div className="min-w-0">
        <div className="text-sm font-medium text-ink">{label}</div>
        {help && <div className="text-xs text-ink-muted">{help}</div>}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={clsx(
          'relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-50',
          checked ? 'bg-brand-600' : 'bg-slate-300',
        )}
      >
        <span
          className={clsx(
            'absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all',
            checked ? 'start-[calc(100%-1.375rem)]' : 'start-0.5',
          )}
        />
      </button>
    </div>
  )
}

/** Save button that shows "saving" and then "saved" for a moment. */
export function SaveButton({
  saving,
  saved,
  disabled,
  label,
  onClick,
  type = 'submit',
}: {
  saving: boolean
  saved?: boolean
  disabled?: boolean
  label?: string
  onClick?: () => void
  type?: 'submit' | 'button'
}) {
  const { t } = useTranslation()
  return (
    <button type={type} className={clsx('btn-primary', saved && !saving && 'bg-emerald-600 hover:bg-emerald-600')} disabled={saving || disabled} onClick={onClick}>
      {saved && !saving && <Check size={16} />}
      {saving ? t('form.saving') : saved ? t('form.saved') : label ?? t('form.save')}
    </button>
  )
}

/** Tracks a "saved" flash after a mutation succeeds. */
export function useSavedFlash(trigger: unknown): boolean {
  const [saved, setSaved] = useState(false)
  useEffect(() => {
    if (!trigger) return
    setSaved(true)
    const timer = window.setTimeout(() => setSaved(false), 2500)
    return () => window.clearTimeout(timer)
  }, [trigger])
  return saved
}

export function InlineError({ message }: { message?: string | null }) {
  if (!message) return null
  return (
    <div role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-700 ring-1 ring-red-200">
      {message}
    </div>
  )
}

export function InlineNotice({ children }: { children: ReactNode }) {
  return <div className="rounded-xl bg-brand-50 px-3 py-2 text-xs text-ink-soft ring-1 ring-brand-100">{children}</div>
}

/** Read-only value with a copy button (webhook URLs and the like). */
export function CopyField({ label, value, help }: { label: string; value: string; help?: string }) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      /* clipboard blocked: the user can still select the text */
    }
  }
  return (
    <div>
      <span className="label">{label}</span>
      <div className="flex items-center gap-2">
        <input className="input text-left font-mono text-xs" dir="ltr" readOnly value={value} onFocus={(e) => e.currentTarget.select()} />
        <button type="button" className="btn-soft shrink-0 px-3" onClick={() => void copy()}>
          {copied ? <Check size={14} /> : <Copy size={14} />}
          {copied ? t('form.copied') : t('form.copy')}
        </button>
      </div>
      {help && <span className="mt-1 block text-xs text-ink-muted">{help}</span>}
    </div>
  )
}

export function SectionCard({ title, subtitle, children, actions }: { title: string; subtitle?: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="card p-5">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-ink">{title}</h2>
          {subtitle && <p className="mt-0.5 text-xs text-ink-soft">{subtitle}</p>}
        </div>
        {actions}
      </div>
      {children}
    </section>
  )
}
