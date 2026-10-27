import { useCallback, useEffect, useMemo, useState } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { CalendarPlus, ChevronLeft, ChevronRight } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { createEvent, deleteEvent, listEvents, updateEvent, type EventInput } from '@/api/calendar'
import { SectionCard } from '@/components/Form'
import { ErrorState } from '@/components/States'
import { errorDetail } from '@/lib/errors'
import { addDays, rangeTitle, shiftAnchor, toBoardEvent, visibleRange, type BoardEvent, type CalendarViewKind } from './dates'
import { EventDialog, emptyEvent, eventToForm, type EventForm } from './EventDialog'
import { MonthView } from './MonthView'
import { TimeGridView } from './TimeGridView'

const VIEWS: CalendarViewKind[] = ['day', 'week', 'month']
const VIEW_STORAGE_KEY = 'maia.calendar.view'

function loadView(): CalendarViewKind {
  try {
    const stored = localStorage.getItem(VIEW_STORAGE_KEY)
    if (stored && (VIEWS as string[]).includes(stored)) return stored as CalendarViewKind
  } catch {
    /* storage unavailable */
  }
  return 'week'
}

function storeView(view: CalendarViewKind) {
  try {
    localStorage.setItem(VIEW_STORAGE_KEY, view)
  } catch {
    /* storage unavailable */
  }
}

/** Ticks once a minute, for "today" and the current-time line. */
function useNow(): Date {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 60_000)
    return () => window.clearInterval(id)
  }, [])
  return now
}

/** Google-Calendar-like board over the tenant's events: month / week / day, create, edit, delete. */
export function CalendarBoard({ tenant, email }: { tenant: string; email?: string }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const now = useNow()
  const [view, setView] = useState<CalendarViewKind>(loadView)
  const [anchor, setAnchor] = useState(() => new Date())
  const [dialog, setDialog] = useState<{ event: BoardEvent | null; initial: EventForm } | null>(null)
  const [error, setError] = useState<string | null>(null)

  const range = useMemo(() => visibleRange(anchor, view), [anchor, view])
  const timeMin = range.start.toISOString()
  const timeMax = range.end.toISOString()
  const events = useQuery({
    queryKey: ['events', tenant, email, timeMin, timeMax],
    queryFn: () => listEvents(tenant, { timeMin, timeMax, email }),
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
  })
  const board = useMemo(() => (events.data ?? []).map(toBoardEvent).filter((ev): ev is BoardEvent => ev !== null), [events.data])

  const closeDialog = useCallback(() => setDialog(null), [])
  const afterWrite = () => {
    setDialog(null)
    setError(null)
    void queryClient.invalidateQueries({ queryKey: ['events', tenant] })
  }
  const save = useMutation({
    mutationFn: ({ id, input }: { id: string | null; input: EventInput }) => (id ? updateEvent(tenant, id, input, email) : createEvent(tenant, input, email)),
    onSuccess: afterWrite,
    onError: (e) => setError(t('form.saveFailed', { reason: errorDetail(e) })),
  })
  const remove = useMutation({
    mutationFn: (id: string) => deleteEvent(tenant, id, email),
    onSuccess: afterWrite,
    onError: (e) => setError(t('calendar.deleteFailed', { reason: errorDetail(e) })),
  })

  const changeView = (next: CalendarViewKind) => {
    setView(next)
    storeView(next)
  }
  const openNew = (day: Date, startMinutes?: number) => {
    setError(null)
    setDialog({ event: null, initial: emptyEvent(day, startMinutes) })
  }
  const openEvent = (ev: BoardEvent) => {
    setError(null)
    setDialog({ event: ev, initial: eventToForm(ev) })
  }
  const selectDay = (day: Date) => {
    setAnchor(day)
    changeView('day')
  }

  const title = rangeTitle(anchor, view)
  const days = view === 'day' ? [range.start] : Array.from({ length: 7 }, (_, i) => addDays(range.start, i))

  return (
    <SectionCard
      title={t('calendar.eventsTitle')}
      subtitle={t('calendar.eventsSubtitle')}
      actions={
        <button type="button" className="btn-primary" onClick={() => openNew(now)}>
          <CalendarPlus size={16} />
          {t('calendar.newEvent')}
        </button>
      }
    >
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <button type="button" className="btn-soft" onClick={() => setAnchor(new Date())}>
          {t('dates.today')}
        </button>
        <div className="flex items-center">
          <button type="button" className="btn-ghost p-1.5" aria-label={t('calendar.prev')} onClick={() => setAnchor((a) => shiftAnchor(a, view, -1))}>
            <ChevronRight size={18} />
          </button>
          <button type="button" className="btn-ghost p-1.5" aria-label={t('calendar.next')} onClick={() => setAnchor((a) => shiftAnchor(a, view, 1))}>
            <ChevronLeft size={18} />
          </button>
        </div>
        <h3 className="text-base font-semibold text-ink">
          {title.ltr && <span dir="ltr">{title.ltr}</span>}
          {title.text}
        </h3>
        {events.isPending && <span className="text-xs text-ink-muted">{t('common.loading')}</span>}
        <div className="ms-auto flex rounded-xl bg-brand-50 p-0.5">
          {VIEWS.map((v) => (
            <button
              key={v}
              type="button"
              className={clsx('rounded-lg px-3 py-1 text-xs font-medium transition-colors', view === v ? 'bg-white text-brand-800 shadow-sm' : 'text-ink-soft hover:text-brand-800')}
              onClick={() => changeView(v)}
            >
              {t(`calendar.views.${v}`)}
            </button>
          ))}
        </div>
      </div>

      {events.isError ? (
        <ErrorState onRetry={() => void events.refetch()} />
      ) : view === 'month' ? (
        <MonthView anchor={anchor} events={board} today={now} onSelectDay={selectDay} onNewEvent={(day) => openNew(day)} onOpenEvent={openEvent} />
      ) : (
        <TimeGridView days={days} events={board} now={now} onNewEvent={openNew} onOpenEvent={openEvent} onSelectDay={selectDay} />
      )}

      {dialog && (
        <EventDialog
          key={dialog.event?.id ?? `new-${dialog.initial.date}-${dialog.initial.start}`}
          event={dialog.event}
          initial={dialog.initial}
          saving={save.isPending}
          deleting={remove.isPending}
          error={error}
          onClose={closeDialog}
          onSave={(input) => save.mutate({ id: dialog.event?.id ?? null, input })}
          onDelete={(id) => remove.mutate(id)}
        />
      )}
    </SectionCard>
  )
}
