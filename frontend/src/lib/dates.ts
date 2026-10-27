import i18n from '@/i18n'

const TZ = 'Asia/Jerusalem'

const timeFormat = new Intl.DateTimeFormat('he-IL', { hour: '2-digit', minute: '2-digit', timeZone: TZ })
const dateFormat = new Intl.DateTimeFormat('he-IL', { day: 'numeric', month: 'numeric', year: '2-digit', timeZone: TZ })
const dayFormat = new Intl.DateTimeFormat('he-IL', { weekday: 'long', day: 'numeric', month: 'long', timeZone: TZ })
const dateTimeFormat = new Intl.DateTimeFormat('he-IL', { dateStyle: 'short', timeStyle: 'short', timeZone: TZ })
const dayKeyFormat = new Intl.DateTimeFormat('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit', timeZone: TZ })

/**
 * The backend stores UTC and serializes it without a zone, in two spellings:
 * "2026-10-18T14:37:25.145000" (REST) and "2026-10-18 14:37:29.173000" (SSE).
 */
export function parseApiDate(value?: string | null): Date | null {
  if (!value) return null
  let s = String(value).trim()
  if (!s) return null
  if (s.includes(' ') && !s.includes('T')) s = s.replace(' ', 'T')
  if (!/(Z|[+-]\d{2}:?\d{2})$/i.test(s)) s += 'Z'
  const date = new Date(s)
  return Number.isNaN(date.getTime()) ? null : date
}

/** "2026-10-18" in Israel time, for grouping messages by day. */
export function dayKey(date: Date): string {
  return dayKeyFormat.format(date)
}

export function formatTime(date: Date): string {
  return timeFormat.format(date)
}

export function formatDateTime(date: Date): string {
  return dateTimeFormat.format(date)
}

function daysApart(a: Date, b: Date): number {
  const [ya, ma, da] = dayKey(a).split('-').map(Number)
  const [yb, mb, db] = dayKey(b).split('-').map(Number)
  return Math.round((Date.UTC(ya, ma - 1, da) - Date.UTC(yb, mb - 1, db)) / 86_400_000)
}

/** List column: time today, "אתמול" yesterday, short date otherwise. */
export function formatListTime(date: Date, now: Date = new Date()): string {
  const diff = daysApart(now, date)
  if (diff === 0) return formatTime(date)
  if (diff === 1) return i18n.t('dates.yesterday')
  return dateFormat.format(date)
}

/** Day separator inside a conversation. */
export function formatDayLabel(date: Date, now: Date = new Date()): string {
  const diff = daysApart(now, date)
  if (diff === 0) return i18n.t('dates.today')
  if (diff === 1) return i18n.t('dates.yesterday')
  return dayFormat.format(date)
}
