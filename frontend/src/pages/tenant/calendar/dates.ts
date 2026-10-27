import type { CalendarEvent } from '@/api/calendar'
import { parseApiDate } from '@/lib/dates'

/**
 * Date helpers for the calendar board. Everything here is browser-local time:
 * the grid, the event form and the hour labels must agree with each other, and
 * the owner's browser runs in Israel time (like the rest of the event form).
 */

export type CalendarViewKind = 'month' | 'week' | 'day'

export interface BoardEvent {
  id: string
  title: string
  start: Date
  /** Exclusive. For all-day events Google gives the day after the last day. */
  end: Date
  allDay: boolean
  location?: string | null
  description?: string | null
}

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/

export const pad = (n: number) => String(n).padStart(2, '0')

export function startOfDay(d: Date): Date {
  const x = new Date(d)
  x.setHours(0, 0, 0, 0)
  return x
}

export function addDays(d: Date, n: number): Date {
  const x = new Date(d)
  x.setDate(x.getDate() + n)
  return x
}

/** Same day of month when it exists, otherwise the last day of the target month. */
export function addMonths(d: Date, n: number): Date {
  const first = new Date(d.getFullYear(), d.getMonth() + n, 1)
  const lastDay = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate()
  first.setDate(Math.min(d.getDate(), lastDay))
  first.setHours(d.getHours(), d.getMinutes(), 0, 0)
  return first
}

/** Weeks start on Sunday (Israel). */
export function startOfWeek(d: Date): Date {
  const x = startOfDay(d)
  return addDays(x, -x.getDay())
}

export function startOfMonth(d: Date): Date {
  const x = startOfDay(d)
  x.setDate(1)
  return x
}

export function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
}

/** "2026-10-25" in browser-local time (the value an <input type="date"> wants). */
export function localDayKey(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

export function minutesOfDay(d: Date): number {
  return d.getHours() * 60 + d.getMinutes()
}

/** The days the board shows for a view; `end` is exclusive. */
export function visibleRange(anchor: Date, view: CalendarViewKind): { start: Date; end: Date } {
  if (view === 'day') {
    const start = startOfDay(anchor)
    return { start, end: addDays(start, 1) }
  }
  if (view === 'week') {
    const start = startOfWeek(anchor)
    return { start, end: addDays(start, 7) }
  }
  const first = startOfMonth(anchor)
  const last = addDays(addMonths(first, 1), -1)
  return { start: startOfWeek(first), end: addDays(startOfWeek(last), 7) }
}

/** Rows of seven days covering the month of `anchor` (4 to 6 rows). */
export function monthWeeks(anchor: Date): Date[][] {
  const { start, end } = visibleRange(anchor, 'month')
  const weeks: Date[][] = []
  for (let d = start; d < end; d = addDays(d, 7)) {
    weeks.push(Array.from({ length: 7 }, (_, i) => addDays(d, i)))
  }
  return weeks
}

export function shiftAnchor(anchor: Date, view: CalendarViewKind, direction: -1 | 1): Date {
  if (view === 'month') return addMonths(anchor, direction)
  return addDays(anchor, view === 'week' ? 7 * direction : direction)
}

// ---------- Events ----------

function parseDateOnly(value: string): Date {
  const [y, m, d] = value.split('-').map(Number)
  return new Date(y, m - 1, d)
}

/** Backend event (RFC3339 with offset, or "YYYY-MM-DD" for all-day) → board event; null when unparsable. */
export function toBoardEvent(ev: CalendarEvent): BoardEvent | null {
  const allDay = DATE_ONLY.test(ev.start)
  const start = allDay ? parseDateOnly(ev.start) : parseApiDate(ev.start)
  const end = allDay ? parseDateOnly(ev.end) : parseApiDate(ev.end)
  if (!start || !end || Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return null
  return {
    id: ev.id,
    title: ev.title || '',
    start,
    end: end < start ? start : end,
    allDay,
    location: ev.location ?? null,
    description: (ev.description as string | null | undefined) ?? null,
  }
}

function overlapsDay(ev: BoardEvent, day: Date): boolean {
  const dayStart = startOfDay(day)
  const dayEnd = addDays(dayStart, 1)
  if (ev.allDay) return ev.start < dayEnd && ev.end > dayStart
  // A timed event that ends exactly at midnight belongs to the previous day only.
  return ev.start < dayEnd && (ev.end > dayStart || sameDay(ev.start, day))
}

/** All-day events first, then by start time. */
export function eventsOnDay(events: BoardEvent[], day: Date): BoardEvent[] {
  return events
    .filter((ev) => overlapsDay(ev, day))
    .sort((a, b) => Number(b.allDay) - Number(a.allDay) || a.start.getTime() - b.start.getTime())
}

export function allDayEventsOn(events: BoardEvent[], day: Date): BoardEvent[] {
  return events.filter((ev) => ev.allDay && overlapsDay(ev, day))
}

export interface DaySegment {
  ev: BoardEvent
  /** Minutes from the start of the day, clipped to [0, 1440]. */
  startMin: number
  endMin: number
}

/** Timed events that touch `day`, clipped to that day (a two-day event gets a segment per day). */
export function timedSegmentsOn(events: BoardEvent[], day: Date): DaySegment[] {
  const dayStart = startOfDay(day)
  const dayEnd = addDays(dayStart, 1)
  const clip = (d: Date) => Math.min(1440, Math.max(0, (d.getTime() - dayStart.getTime()) / 60_000))
  return events
    .filter((ev) => !ev.allDay && overlapsDay(ev, day))
    .map((ev) => ({ ev, startMin: clip(ev.start), endMin: ev.end > dayEnd ? 1440 : clip(ev.end) }))
    .sort((a, b) => a.startMin - b.startMin)
}

export interface Placed<T> {
  item: T
  col: number
  cols: number
}

/**
 * Side-by-side layout for overlapping items (what Google Calendar does): items that
 * overlap form a cluster; each gets the first free column, and every item of the
 * cluster is as wide as 1 / (number of columns in the cluster).
 */
export function placeOverlapping<T>(items: T[], range: (item: T) => [number, number]): Placed<T>[] {
  const sorted = [...items].sort((a, b) => range(a)[0] - range(b)[0] || range(b)[1] - range(a)[1])
  const out: Placed<T>[] = []
  let cluster: { placed: Placed<T>[]; colEnds: number[]; end: number } | null = null
  const close = () => {
    if (!cluster) return
    for (const p of cluster.placed) p.cols = cluster.colEnds.length
  }
  for (const item of sorted) {
    const [s, e] = range(item)
    if (!cluster || s >= cluster.end) {
      close()
      cluster = { placed: [], colEnds: [], end: e }
    }
    let col = cluster.colEnds.findIndex((end) => end <= s)
    if (col === -1) {
      col = cluster.colEnds.length
      cluster.colEnds.push(e)
    } else cluster.colEnds[col] = e
    const placed = { item, col, cols: 1 }
    cluster.placed.push(placed)
    cluster.end = Math.max(cluster.end, e)
    out.push(placed)
  }
  close()
  return out
}

// ---------- Labels (browser-local time, Hebrew) ----------

const timeFmt = new Intl.DateTimeFormat('he-IL', { hour: '2-digit', minute: '2-digit' })
const monthYearFmt = new Intl.DateTimeFormat('he-IL', { month: 'long', year: 'numeric' })
const dayMonthFmt = new Intl.DateTimeFormat('he-IL', { day: 'numeric', month: 'long' })
const dayMonthYearFmt = new Intl.DateTimeFormat('he-IL', { day: 'numeric', month: 'long', year: 'numeric' })
const fullDayFmt = new Intl.DateTimeFormat('he-IL', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })

export function fmtTime(d: Date): string {
  return timeFmt.format(d)
}

/**
 * Title of the toolbar. `ltr` is a numeric range ("19–25") that must be rendered
 * left-to-right so the bidi algorithm does not flip it; `text` follows it.
 */
export function rangeTitle(anchor: Date, view: CalendarViewKind): { ltr?: string; text: string } {
  if (view === 'month') return { text: monthYearFmt.format(anchor) }
  if (view === 'day') return { text: fullDayFmt.format(anchor) }
  const start = startOfWeek(anchor)
  const end = addDays(start, 6)
  if (start.getMonth() === end.getMonth()) return { ltr: `${start.getDate()}–${end.getDate()}`, text: ` ב${monthYearFmt.format(end)}` }
  if (start.getFullYear() === end.getFullYear()) return { text: `${dayMonthFmt.format(start)} – ${dayMonthYearFmt.format(end)}` }
  return { text: `${dayMonthYearFmt.format(start)} – ${dayMonthYearFmt.format(end)}` }
}
