import { api } from './client'

const enc = encodeURIComponent

export type DayKey = 'sun' | 'mon' | 'tue' | 'wed' | 'thu' | 'fri' | 'sat'
export const DAY_KEYS: DayKey[] = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat']

export interface OpeningHours {
  day: DayKey
  start_time: string
  end_time: string
  closed: boolean
}

export const DEFAULT_OPENING_HOURS: OpeningHours[] = [
  { day: 'sun', start_time: '09:00', end_time: '17:00', closed: false },
  { day: 'mon', start_time: '09:00', end_time: '17:00', closed: false },
  { day: 'tue', start_time: '09:00', end_time: '17:00', closed: false },
  { day: 'wed', start_time: '09:00', end_time: '17:00', closed: false },
  { day: 'thu', start_time: '09:00', end_time: '17:00', closed: false },
  { day: 'fri', start_time: '09:00', end_time: '13:00', closed: false },
  { day: 'sat', start_time: '09:00', end_time: '13:00', closed: true },
]

/** A Google account connected to the tenant (calendar and/or Gmail scopes). */
export interface GoogleConnection {
  email: string
  default: boolean
  scopes: string[]
}

/** Raw stored record (tokens are present in the payload; never render them). */
export interface GoogleAccountDetail {
  email: string
  default?: boolean
  opening_hours?: Partial<OpeningHours>[]
  ignore_long_events?: boolean
  scopes?: string[]
  gmail_watch_enabled?: boolean
  created_at?: string | null
  expiration?: string | null
}

export type GoogleScope = 'calendar' | 'email'

export async function listGoogleConnections(tenant: string, scope?: GoogleScope): Promise<GoogleConnection[]> {
  const { data } = await api.get<GoogleConnection[]>(`/management/v1/calendar/connected/${enc(tenant)}`, {
    params: { scope_type: scope },
  })
  return data
}

export async function listGoogleAccountDetails(tenant: string): Promise<GoogleAccountDetail[]> {
  const { data } = await api.get<Record<string, unknown>[]>(`/management/v1/calendar/all/${enc(tenant)}`)
  // keep only what the UI needs; drop tokens on the client side
  return data.map((raw) => ({
    email: String(raw.email ?? ''),
    default: Boolean(raw.default),
    opening_hours: (raw.opening_hours as Partial<OpeningHours>[] | undefined) ?? [],
    ignore_long_events: Boolean(raw.ignore_long_events),
    scopes: (raw.scopes as string[] | undefined) ?? [],
    gmail_watch_enabled: Boolean(raw.gmail_watch_enabled),
    created_at: (raw.created_at as string | null | undefined) ?? null,
    expiration: (raw.expiration as string | null | undefined) ?? null,
  }))
}

/** URL of Google's consent screen; open it in a popup, the callback posts `auth_success` to the opener. */
export async function getGoogleAuthorizationUrl(tenant: string, scope: GoogleScope, pubSub = false): Promise<string> {
  const { data } = await api.get<{ authorization_url: string }>(`/management/v1/calendar/authorization-url/${enc(tenant)}`, {
    params: { scopes: scope, pub_sub: pubSub },
  })
  return data.authorization_url
}

export async function disconnectGoogle(tenant: string, email: string, scope?: GoogleScope): Promise<void> {
  await api.delete(`/management/v1/calendar/disconnect/${enc(tenant)}/${enc(email)}`, { params: { scope } })
}

export async function updateGoogleCalendar(
  tenant: string,
  email: string,
  patch: { opening_hours?: OpeningHours[]; default?: boolean; ignore_long_events?: boolean },
): Promise<void> {
  await api.patch(`/management/v1/calendar/update/${enc(tenant)}/${enc(email)}`, patch, { params: { email } })
}

// ---------- Events ----------

export interface CalendarEvent {
  id: string
  title: string
  start: string
  end: string
  location?: string | null
  description?: string | null
  guests?: { email?: string; displayName?: string }[] | null
  [key: string]: unknown
}

export interface EventInput {
  start: string
  end: string
  summary: string
  location?: string
  description?: string
  guests?: string[]
  add_meet_link?: boolean
}

export async function listEvents(tenant: string, opts: { timeMin?: string; timeMax?: string; maxResults?: number; email?: string } = {}): Promise<CalendarEvent[]> {
  const { data } = await api.get<CalendarEvent[]>(`/management/v1/calendar/events/${enc(tenant)}`, { params: opts })
  return Array.isArray(data) ? data : []
}

export async function createEvent(tenant: string, input: EventInput, email?: string): Promise<unknown> {
  const { data } = await api.post(`/management/v1/calendar/events/${enc(tenant)}`, input, { params: { email } })
  return data
}

export async function updateEvent(tenant: string, eventId: string, input: EventInput, email?: string): Promise<unknown> {
  const { data } = await api.patch(`/management/v1/calendar/events/${enc(tenant)}/${enc(eventId)}`, input, { params: { email } })
  return data
}

export async function deleteEvent(tenant: string, eventId: string, email?: string): Promise<void> {
  await api.delete(`/management/v1/calendar/events/${enc(tenant)}/${enc(eventId)}`, { params: { email } })
}
