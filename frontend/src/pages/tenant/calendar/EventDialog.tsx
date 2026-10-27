import { useEffect, useState, type FormEvent } from 'react'
import { Trash2, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { EventInput } from '@/api/calendar'
import { Field, InlineError, InlineNotice, SaveButton } from '@/components/Form'
import { localDayKey, pad, type BoardEvent } from './dates'

export interface EventForm {
  summary: string
  date: string
  start: string
  end: string
  location: string
  description: string
}

const hm = (minutes: number) => `${pad(Math.floor(minutes / 60))}:${pad(minutes % 60)}`

/** A blank form for `day`, starting at `startMinutes` (default 10:00) for half an hour. */
export function emptyEvent(day: Date = new Date(), startMinutes = 10 * 60): EventForm {
  const start = Math.min(Math.max(0, startMinutes), 23 * 60 + 30)
  const end = Math.min(start + 30, 23 * 60 + 59)
  return { summary: '', date: localDayKey(day), start: hm(start), end: hm(end), location: '', description: '' }
}

export function eventToForm(ev: BoardEvent): EventForm {
  const time = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return {
    summary: ev.title,
    date: localDayKey(ev.start),
    start: ev.allDay ? '00:00' : time(ev.start),
    end: ev.allDay ? '23:59' : time(ev.end),
    location: ev.location ?? '',
    description: ev.description ?? '',
  }
}

/** Local wall-clock → RFC3339 with the browser's offset for that instant. */
function toIso(date: string, time: string): string {
  const local = new Date(`${date}T${time}:00`)
  const offsetMin = -local.getTimezoneOffset()
  const sign = offsetMin >= 0 ? '+' : '-'
  const abs = Math.abs(offsetMin)
  return `${date}T${time}:00${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
}

export function formToInput(form: EventForm): EventInput {
  return {
    summary: form.summary.trim(),
    start: toIso(form.date, form.start),
    end: toIso(form.date, form.end),
    location: form.location.trim() || undefined,
    description: form.description.trim() || undefined,
    guests: [],
  }
}

export function EventDialog({
  event,
  initial,
  saving,
  deleting,
  error,
  onClose,
  onSave,
  onDelete,
}: {
  /** The event being edited; null when creating. */
  event: BoardEvent | null
  initial: EventForm
  saving: boolean
  deleting: boolean
  error: string | null
  onClose: () => void
  onSave: (input: EventInput) => void
  onDelete: (id: string) => void
}) {
  const { t } = useTranslation()
  const [form, setForm] = useState<EventForm>(initial)
  // All-day events cannot be written back through the API (it only sends dateTime), so they are delete-only here.
  const allDay = event?.allDay ?? false
  const valid = form.summary.trim().length > 0 && form.end > form.start

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  function submit(e: FormEvent) {
    e.preventDefault()
    if (!valid || allDay) return
    onSave(formToInput(form))
  }

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-labelledby="event-dialog-title">
      <div className="absolute inset-0 bg-ink/30" onClick={onClose} />
      <form onSubmit={submit} className="relative w-full max-w-md space-y-3 rounded-2xl bg-white p-5 shadow-pop">
        <div className="flex items-center justify-between">
          <h3 id="event-dialog-title" className="text-base font-semibold text-ink">
            {event ? t('calendar.editEvent') : t('calendar.newEvent')}
          </h3>
          <button type="button" className="btn-ghost p-1.5" onClick={onClose} aria-label={t('common.close')}>
            <X size={16} />
          </button>
        </div>
        <InlineError message={error} />
        {allDay && <InlineNotice>{t('calendar.allDayReadOnly')}</InlineNotice>}
        <Field label={t('calendar.summary')}>
          <input className="input" value={form.summary} onChange={(e) => setForm({ ...form, summary: e.target.value })} dir="auto" required autoFocus disabled={allDay} />
        </Field>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label={t('calendar.date')}>
            <input type="date" dir="ltr" className="input" value={form.date} onChange={(e) => setForm({ ...form, date: e.target.value })} required disabled={allDay} />
          </Field>
          {!allDay && (
            <>
              <Field label={t('calendar.start')}>
                <input type="time" dir="ltr" className="input" value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} required />
              </Field>
              <Field label={t('calendar.end')}>
                <input type="time" dir="ltr" className="input" value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} required />
              </Field>
            </>
          )}
        </div>
        <Field label={t('calendar.location')}>
          <input className="input" value={form.location} onChange={(e) => setForm({ ...form, location: e.target.value })} dir="auto" disabled={allDay} />
        </Field>
        <Field label={t('calendar.description')}>
          <textarea className="input min-h-[4rem]" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} dir="auto" disabled={allDay} />
        </Field>
        <div className="flex flex-wrap items-center gap-2 pt-1">
          {event && (
            <button
              type="button"
              className="btn-ghost text-red-600 hover:bg-red-50"
              disabled={deleting || saving}
              onClick={() => {
                if (window.confirm(t('calendar.deleteConfirm', { title: event.title }))) onDelete(event.id)
              }}
            >
              <Trash2 size={14} />
              {deleting ? t('common.loading') : t('calendar.deleteEvent')}
            </button>
          )}
          <div className="ms-auto flex gap-2">
            <button type="button" className="btn-ghost" onClick={onClose}>
              {t('knowledge.qa.cancel')}
            </button>
            {!allDay && <SaveButton saving={saving} disabled={!valid} />}
          </div>
        </div>
      </form>
    </div>
  )
}
