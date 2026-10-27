import { useState, type FormEvent, type KeyboardEvent } from 'react'
import { SendHorizontal } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface Props {
  disabled?: boolean
  disabledReason?: string
  hint?: string
  sending: boolean
  error?: string | null
  onSend: (text: string) => Promise<void>
}

/** Manual reply box. Enter sends, Shift+Enter adds a line. */
export function Composer({ disabled, disabledReason, hint, sending, error, onSend }: Props) {
  const { t } = useTranslation()
  const [text, setText] = useState('')

  async function submit(event?: FormEvent) {
    event?.preventDefault()
    const value = text.trim()
    if (!value || sending || disabled) return
    await onSend(value)
    setText('')
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      void submit()
    }
  }

  if (disabled) {
    return <div className="border-t border-brand-100 bg-brand-50 px-4 py-3 text-center text-xs text-ink-soft">{disabledReason}</div>
  }

  return (
    <form onSubmit={submit} className="border-t border-brand-100 bg-white p-3">
      {hint && <div className="mb-2 text-xs text-ink-muted">{hint}</div>}
      {error && (
        <div role="alert" className="mb-2 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-700 ring-1 ring-red-200">
          {error}
        </div>
      )}
      <div className="flex items-end gap-2">
        <textarea
          className="input max-h-40 min-h-[2.75rem] flex-1 resize-none"
          rows={Math.min(5, Math.max(1, text.split('\n').length))}
          placeholder={t('inbox.compose.placeholder')}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={sending}
          dir="auto"
        />
        <button type="submit" className="btn-primary h-11 shrink-0 px-4" disabled={sending || !text.trim()}>
          <SendHorizontal size={18} className="rtl:-scale-x-100" />
          <span className="hidden sm:inline">{sending ? t('inbox.compose.sending') : t('inbox.compose.send')}</span>
        </button>
      </div>
    </form>
  )
}
