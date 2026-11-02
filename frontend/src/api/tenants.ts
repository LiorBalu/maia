import { api } from './client'

export type Channel = 'whatsapp' | 'email' | 'telephone'

export interface WahaAccountSummary {
  phone_number: string
  is_connected?: boolean
  session_status?: string | null
}

export interface TenantSummary {
  name: string
  description?: string | null
  status: string
  default_language?: string | null
  gpt_model?: string | null
  manual_mode?: boolean | null
  waha_accounts?: WahaAccountSummary[] | null
}

export async function listTenants(): Promise<TenantSummary[]> {
  const { data } = await api.get<TenantSummary[]>('/management/v1/tenants')
  return data
}

export async function getTenant(name: string): Promise<TenantSummary> {
  const { data } = await api.get<TenantSummary>(`/management/v1/tenants/${encodeURIComponent(name)}`)
  return data
}

/** Which channels look connected, from the summary the list endpoint returns (WhatsApp when a WAHA number is linked). */
export function tenantChannels(tenant: TenantSummary): Channel[] {
  return tenant.waha_accounts && tenant.waha_accounts.length > 0 ? ['whatsapp'] : []
}
