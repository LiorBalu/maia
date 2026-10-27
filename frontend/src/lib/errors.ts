/** Human-readable reason from an axios error (FastAPI `detail`, validation errors, or the message). */
export function errorDetail(error: unknown): string {
  const data = (error as { response?: { data?: unknown } })?.response?.data as { detail?: unknown; message?: string } | undefined
  const detail = data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const loc = Array.isArray((item as { loc?: unknown[] }).loc) ? (item as { loc: unknown[] }).loc.slice(1).join('.') : ''
        const msg = (item as { msg?: string }).msg ?? ''
        return loc ? `${loc}: ${msg}` : msg
      })
      .join('; ')
  }
  if (typeof data?.message === 'string') return data.message
  return (error as Error)?.message || ''
}
