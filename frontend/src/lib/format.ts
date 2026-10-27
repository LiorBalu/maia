const dateTime = new Intl.DateTimeFormat('he-IL', {
  dateStyle: 'short',
  timeStyle: 'short',
  timeZone: 'Asia/Jerusalem',
})

/** ISO string → "18.10.2026, 14:05" in Israel time; empty when missing or invalid. */
export function formatDateTime(value?: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : dateTime.format(date)
}
