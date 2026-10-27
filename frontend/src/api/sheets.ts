import { api } from './client'

const enc = encodeURIComponent

export interface SheetPreview {
  title: string
  headers: string[]
  rows: Record<string, string>[]
  row_count: number
}

/** The tenant's connected Google Sheet (read by the bot's st_search_sheet tool). */
export interface SheetConnection {
  sheet_id: string | null
  sheet_url: string | null
  /** Address the business has to share the sheet with. */
  service_account: string
  preview: SheetPreview | null
  /** Set when a sheet is connected but Google refused to read it (not shared, deleted...). */
  error: string | null
}

export async function getSheet(tenant: string): Promise<SheetConnection> {
  const { data } = await api.get<SheetConnection>(`/management/v1/sheets/${enc(tenant)}`)
  return data
}

/** Saves the sheet (link or id) after the backend verified it can read it. */
export async function connectSheet(tenant: string, url: string): Promise<SheetConnection> {
  const { data } = await api.put<SheetConnection>(`/management/v1/sheets/${enc(tenant)}`, { url })
  return data
}

export async function disconnectSheet(tenant: string): Promise<SheetConnection> {
  const { data } = await api.delete<SheetConnection>(`/management/v1/sheets/${enc(tenant)}`)
  return data
}
