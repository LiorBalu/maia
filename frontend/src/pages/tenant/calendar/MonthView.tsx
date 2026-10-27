import clsx from 'clsx'
import { useTranslation } from 'react-i18next'

import { DAY_KEYS } from '@/api/calendar'
import { eventsOnDay, fmtTime, monthWeeks, sameDay, type BoardEvent } from './dates'

const MAX_CHIPS = 3

export function MonthView({
  anchor,
  events,
  today,
  onSelectDay,
  onNewEvent,
  onOpenEvent,
}: {
  anchor: Date
  events: BoardEvent[]
  today: Date
  onSelectDay: (day: Date) => void
  onNewEvent: (day: Date) => void
  onOpenEvent: (ev: BoardEvent) => void
}) {
  const { t } = useTranslation()
  const weeks = monthWeeks(anchor)

  return (
    <div className="overflow-hidden rounded-xl ring-1 ring-brand-100">
      <div className="grid grid-cols-7 border-b border-brand-100 bg-surface text-center text-xs font-medium text-ink-soft">
        {DAY_KEYS.map((key) => (
          <div key={key} className="py-2">
            {t(`calendar.days.${key}`)}
          </div>
        ))}
      </div>
      {weeks.map((week) => (
        <div key={week[0].toISOString()} className="grid grid-cols-7 border-b border-brand-100 last:border-b-0">
          {week.map((day) => {
            const inMonth = day.getMonth() === anchor.getMonth()
            const isToday = sameDay(day, today)
            const items = eventsOnDay(events, day)
            const shown = items.slice(0, MAX_CHIPS)
            const extra = items.length - shown.length
            return (
              <div
                key={day.toISOString()}
                className={clsx('min-h-[6.5rem] cursor-pointer border-e border-brand-100 p-1 transition-colors last:border-e-0 hover:bg-brand-50/60', !inMonth && 'bg-surface/70')}
                onClick={() => onNewEvent(day)}
              >
                <div className="mb-1 flex justify-center">
                  <button
                    type="button"
                    className={clsx(
                      'grid h-6 w-6 place-items-center rounded-full text-xs font-medium transition-colors',
                      isToday ? 'bg-brand-600 text-white hover:bg-brand-700' : inMonth ? 'text-ink hover:bg-brand-100' : 'text-ink-muted hover:bg-brand-100',
                    )}
                    onClick={(e) => {
                      e.stopPropagation()
                      onSelectDay(day)
                    }}
                  >
                    {day.getDate()}
                  </button>
                </div>
                <div className="space-y-0.5">
                  {shown.map((ev) => (
                    <button
                      key={ev.id}
                      type="button"
                      title={ev.title}
                      className={clsx(
                        'flex w-full items-center gap-1 rounded px-1 py-px text-start text-[11px] leading-4 transition-colors',
                        ev.allDay ? 'bg-brand-600 text-white hover:bg-brand-700' : 'text-ink hover:bg-brand-100',
                      )}
                      onClick={(e) => {
                        e.stopPropagation()
                        onOpenEvent(ev)
                      }}
                    >
                      {!ev.allDay && (
                        <>
                          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-brand-600" />
                          <span className="shrink-0 text-ink-soft" dir="ltr">
                            {fmtTime(ev.start)}
                          </span>
                        </>
                      )}
                      <span className="min-w-0 truncate" dir="auto">
                        {ev.title}
                      </span>
                    </button>
                  ))}
                  {extra > 0 && (
                    <button
                      type="button"
                      className="w-full px-1 text-start text-[11px] font-medium text-ink-soft hover:text-brand-800"
                      onClick={(e) => {
                        e.stopPropagation()
                        onSelectDay(day)
                      }}
                    >
                      {t('calendar.more', { count: extra })}
                    </button>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      ))}
    </div>
  )
}
