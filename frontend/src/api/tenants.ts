import { api } from './client'

export type Channel = 'whatsapp' | 'email' | 'telephone'

/** Twilio Voice credentials (call recordings); WhatsApp numbers live in `waha_accounts`. */
export interface TwilioAccount {
  account_sid: string
  phone_number: string
}

export interface WahaAccountSummary {
  phone_number: string
  is_connected?: boolean
  session_status?: string | null
}

export interface PlatformBotSetting {
  platform: string
  mode?: string
}

export interface TenantSummary {
  name: string
  description?: string | null
  status: string
  default_language?: string | null
  default_timezone?: string | null
  gpt_model?: string | null
  manual_mode?: boolean | null
  twilio_accounts?: TwilioAccount[] | null
  waha_accounts?: WahaAccountSummary[] | null
  platform_bot_settings?: PlatformBotSetting[] | null
}

export async function listTenants(): Promise<TenantSummary[]> {
  const { data } = await api.get<TenantSummary[]>('/management/v1/tenants')
  return data
}

export async function getTenant(name: string): Promise<TenantSummary> {
  const { data } = await api.get<TenantSummary>(`/management/v1/tenants/${encodeURIComponent(name)}`)
  return data
}

/** Which channels look connected, from the summary the list endpoint returns. */
export function tenantChannels(tenant: TenantSummary): Channel[] {
  const found = new Set<Channel>()
  if (tenant.waha_accounts && tenant.waha_accounts.length > 0) found.add('whatsapp')
  for (const setting of tenant.platform_bot_settings ?? []) {
    if (setting.platform === 'waha') found.add('whatsapp')
    else if (setting.platform === 'email' || setting.platform === 'telephone') found.add(setting.platform)
  }
  return (['whatsapp', 'email', 'telephone'] as Channel[]).filter((c) => found.has(c))
}
