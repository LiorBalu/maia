const KEY = 'maia_public_backend_url'

/** The public address of the backend (ngrok in development), used only to compose webhook URLs to copy. */
export function getPublicBackendUrl(): string {
  try {
    return localStorage.getItem(KEY) || ''
  } catch {
    return ''
  }
}

export function setPublicBackendUrl(value: string) {
  try {
    localStorage.setItem(KEY, value.trim().replace(/\/+$/, ''))
  } catch {
    /* ignore */
  }
}

export function webhookUrl(path: string): string {
  const base = getPublicBackendUrl() || 'https://<כתובת-ציבורית>'
  return `${base}${path}`
}
