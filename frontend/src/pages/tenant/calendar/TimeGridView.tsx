import { useEffect, useRef } from 'react'
import clsx from 'clsx'
import { useTranslation } from 'react-i18next'

import { DAY_KEYS } from '@/api/calendar'
import { allDayEventsOn, fmtTime, minutesOfDay, pad, placeOverlapping, sameDay, timedSegmentsOn, type BoardEvent, type DaySegment } from './dates'

/** Pixels per hour in the time grid. */
const HOUR_PX = 48
const HOURS = Array.from({ length: 24 }, (_, h) => h)
/** The grid opens scrolled to this hour, like Google Calendar's morning. */
const FIRST_VISIBLE_HOUR = 7

/** Week view (seven days) or day view (one day): header, all-day row, scrollable hour grid. */
export function TimeGridView({
  days,
  events,
  now,
  onNewEvent,
  onOpenEvent,
  onSelectDay,
}: {
  days: Date[]
  events: BoardEvent[]
  now: Date
  onNewEvent: (day: Date, startMinutes: number) => void
  onOpenEvent: (ev: BoardEvent) => void
  onSelectDay: (day: Date) => void
}) {
  const { t } = useTranslation()
  const scrollRef = useRef<HTMLDivElement>(null)
  const gridStyle = { gridTemplateColumns: `3.5rem repeat(${days.length}, minmax(0, 1fr))` }
  const allDayByDay = days.map((day) => allDayEventsOn(events, day))
  const hasAllDay = allDayByDay.some((list) => list.length > 0)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: FIRST_VISIBLE_HOUR * HOUR_PX })
  }, [days.length])

  return (
    <div className="overflow-hidden rounded-xl ring-1 ring-brand-100">
      <div className="grid border-b border-brand-100 bg-surface" style={gridStyle}>
        <div />
        {days.map((day) => {
          const isToday = sameDay(day, now)
          return (
            <div key={day.toISOString()} className="flex flex-col items-center gap-0.5 border-s border-brand-100 py-2">
              <span className={clsx('text-[11px] font-medium', isToday ? 'text-brand-700' : 'text-ink-soft')}>{t(`calendar.days.${DAY_KEYS[day.getDay()]}`)}</span>
              <button
                type="button"
                className={clsx('grid h-7 w-7 place-items-center rounded-full text-sm font-semibold transition-colors', isToday ? 'bg-brand-600 text-white hover:bg-brand-700' : 'text-ink hover:bg-brand-100')}
                onClick={() => onSelectDay(day)}
              >
                {day.getDate()}
              </button>
            </div>
          )
        })}
      </div>

      {hasAllDay && (
        <div className="grid border-b border-brand-100" style={gridStyle}>
          <div className="px-1 py-1.5 text-end text-[10px] text-ink-muted">{t('calendar.allDay')}</div>
          {days.map((day, i) => (
            <div key={day.toISOString()} className="min-h-[1.75rem] space-y-0.5 border-s border-brand-100 p-0.5">
              {allDayByDay[i].map((ev) => (
                <button
                  key={ev.id}
                  type="button"
                  title={ev.title}
                  className="block w-full truncate rounded bg-brand-600 px-1.5 py-px text-start text-[11px] leading-4 text-white hover:bg-brand-700"
                  onClick={() => onOpenEvent(ev)}
                  dir="auto"
                >
                  {ev.title}
                </button>
              ))}
            </div>
          ))}
        </div>
      )}

      <div ref={scrollRef} className="max-h-[65vh] min-h-[24rem] overflow-y-auto">
        <div className="grid" style={{ ...gridStyle, height: 24 * HOUR_PX }}>
          <div className="relative">
            {HOURS.filter((h) => h > 0).map((h) => (
              <span key={h} className="absolute end-1.5 -translate-y-1/2 text-[10px] text-ink-muted" style={{ top: h * HOUR_PX }} dir="ltr">
                {pad(h)}:00
              </span>
            ))}
          </div>
          {days.map((day) => (
            <DayColumn key={day.toISOString()} day={day} segments={timedSegmentsOn(events, day)} isToday={sameDay(day, now)} now={now} onNewEvent={onNewEvent} onOpenEvent={onOpenEvent} />
          ))}
        </div>
      </div>
    </div>
  )
}

function DayColumn({
  day,
  segments,
  isToday,
  now,
  onNewEvent,
  onOpenEvent,
}: {
  day: Date
  segments: DaySegment[]
  isToday: boolean
  now: Date
  onNewEvent: (day: Date, startMinutes: number) => void
  onOpenEvent: (ev: BoardEvent) => void
}) {
  // Short events get a 30-minute footprint for the layout so neighbours do not sit on top of them.
  const placed = placeOverlapping(segments, (seg) => [seg.startMin, Math.max(seg.endMin, seg.startMin + 30)])

  return (
    <div
      className="relative cursor-pointer border-s border-brand-100"
      onClick={(e) => {
        if (e.target !== e.currentTarget) return
        const rect = e.currentTarget.getBoundingClientRect()
        const minutes = Math.floor(((e.clientY - rect.top) / HOUR_PX) * 2) * 30
        onNewEvent(day, Math.min(Math.max(0, minutes), 23 * 60 + 30))
      }}
    >
      {HOURS.filter((h) => h > 0).map((h) => (
        <div key={h} className="pointer-events-none absolute inset-x-0 border-t border-brand-100/80" style={{ top: h * HOUR_PX }} />
      ))}
      {placed.map(({ item, col, cols }) => {
        const top = (item.startMin / 60) * HOUR_PX
        const height = Math.max(((item.endMin - item.startMin) / 60) * HOUR_PX, 22)
        return (
          <button
            key={`${item.ev.id}-${item.startMin}`}
            type="button"
            title={item.ev.title}
            className="absolute overflow-hidden rounded-md border-s-[3px] border-brand-600 bg-brand-100 px-1.5 py-0.5 text-start text-[11px] leading-4 text-brand-900 transition-colors hover:bg-brand-200"
            style={{ top, height, insetInlineStart: `calc(${(col / cols) * 100}% + 2px)`, width: `calc(${100 / cols}% - 4px)` }}
            onClick={(e) => {
              e.stopPropagation()
              onOpenEvent(item.ev)
            }}
          >
            <div className="truncate font-semibold" dir="auto">
              {item.ev.title}
            </div>
            {height >= 36 && (
              <div className="truncate text-brand-800" dir="ltr">
                {fmtTime(item.ev.start)}–{fmtTime(item.ev.end)}
              </div>
            )}
          </button>
        )
      })}
      {isToday && (
        <div className="pointer-events-none absolute inset-x-0 z-10 flex items-center" style={{ top: (minutesOfDay(now) / 60) * HOUR_PX }}>
          <span className="-ms-1 h-2.5 w-2.5 rounded-full bg-red-500" />
          <span className="h-0.5 flex-1 bg-red-500" />
        </div>
      )}
    </div>
  )
}
