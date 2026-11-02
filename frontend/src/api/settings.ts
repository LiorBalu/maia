import { api } from './client'
import type { TenantSummary } from './tenants'

const enc = encodeURIComponent

// ---------- Tenant (general) ----------

export interface TenantFeature {
  name: string
  enabled: boolean
  meta_data?: Record<string, unknown>
}

export interface TenantDetail extends TenantSummary {
  features?: TenantFeature[] | null
  access_users?: string[]
  creator_email?: string | null
  phone_numbers?: string[]
  /** Names of the registered tools the bot may call (Bot > Tools). */
  tools?: string[]
}

export interface TenantUpdate {
  description?: string
  default_language?: string
  manual_mode?: boolean
  features?: TenantFeature[]
  status?: string
  tools?: string[]
}

export async function getTenantDetail(tenant: string): Promise<TenantDetail> {
  const { data } = await api.get<TenantDetail>(`/management/v1/tenants/${enc(tenant)}`)
  return data
}

/** PUT on an existing tenant applies only the fields sent. */
export async function updateTenant(tenant: string, patch: TenantUpdate): Promise<void> {
  await api.put(`/management/v1/tenants/${enc(tenant)}`, { name: tenant, ...patch })
}

// ---------- Chat settings ----------

export interface SettingsFeature {
  name: string
  enabled: boolean
  meta_data?: Record<string, unknown>
  options?: Record<string, unknown>
  description?: string
  he_description?: string
}

export interface ChatSettings {
  tenant_name: string
  chatbot_human_like_delay: boolean
  chatbot_human_like_time: number
  features?: SettingsFeature[]
  [key: string]: unknown
}

export async function getChatSettings(tenant: string): Promise<ChatSettings> {
  const { data } = await api.get<ChatSettings>(`/management/v1/settings/chat/${enc(tenant)}`)
  return data
}

export async function updateChatSettings(tenant: string, settings: ChatSettings): Promise<ChatSettings> {
  const { data } = await api.put<ChatSettings>(`/management/v1/settings/chat/${enc(tenant)}`, settings)
  return data
}

// ---------- Call settings (phone) ----------

export interface RealtimeConf {
  prefix_padding_ms: number
  silence_duration_ms: number
  temperature: number
  audio_transcription_model?: string
  audio_transcription_language?: string | null
  noise_reduction_mode?: 'near_field' | 'far_field' | 'none' | string | null
}

export interface CallSettings {
  tenant_name: string
  phone_number: string
  tts_provider: 'realtime'
  gpt_voice: string
  openai_speed: number
  realtime_conf: RealtimeConf
}

export const GPT_VOICES = ['marin', 'cedar', 'alloy', 'ash', 'ballad', 'coral', 'echo', 'sage', 'shimmer', 'verse']

export async function getCallSettings(tenant: string): Promise<CallSettings> {
  const { data } = await api.get<CallSettings>(`/management/v1/settings/call/${enc(tenant)}`)
  return data
}

export async function updateCallSettings(tenant: string, settings: CallSettings): Promise<CallSettings> {
  const { data } = await api.put<CallSettings>(`/management/v1/settings/call/${enc(tenant)}`, settings)
  return data
}

// ---------- WAHA (self-hosted WhatsApp, trial) ----------

export interface WahaAccount {
  phone_number: string
  webhook_url?: string | null
  is_connected: boolean
  session_status?: string | null
  last_status_update?: string | null
}

export async function listWahaAccounts(tenant: string): Promise<WahaAccount[]> {
  const { data } = await api.get<WahaAccount[]>(`/management/v1/waha/${enc(tenant)}/accounts`)
  return data
}

export async function createWahaAccount(tenant: string, phoneNumber: string): Promise<WahaAccount> {
  const { data } = await api.post<WahaAccount>(`/management/v1/waha/${enc(tenant)}/accounts`, { phone_number: phoneNumber })
  return data
}

export async function deleteWahaAccount(tenant: string, phoneNumber: string): Promise<void> {
  await api.delete(`/management/v1/waha/${enc(tenant)}/accounts/${enc(phoneNumber)}`)
}

export async function createWahaClient(tenant: string, phoneNumber: string): Promise<unknown> {
  const { data } = await api.post(`/management/v1/waha/${enc(tenant)}/accounts/${enc(phoneNumber)}/create-client`)
  return data
}

export interface WahaQr {
  mimetype?: string
  data?: string
  qrCode?: string
  [key: string]: unknown
}

export async function getWahaQr(tenant: string, phoneNumber: string): Promise<WahaQr> {
  const { data } = await api.get<WahaQr>(`/management/v1/waha/${enc(tenant)}/accounts/${enc(phoneNumber)}/qr`)
  return data
}

export async function getWahaStatus(tenant: string, phoneNumber: string): Promise<Record<string, unknown>> {
  const { data } = await api.get<Record<string, unknown>>(`/management/v1/waha/${enc(tenant)}/accounts/${enc(phoneNumber)}/status`)
  return data
}

export async function disconnectWaha(tenant: string, phoneNumber: string): Promise<void> {
  await api.post(`/management/v1/waha/${enc(tenant)}/accounts/${enc(phoneNumber)}/disconnect`)
}

// ---------- Gmail ----------

export async function registerEmailStream(tenant: string, email: string): Promise<{ message?: string }> {
  const { data } = await api.post<{ message?: string }>('/management/v1/email/register/stream', { email }, { params: { tenant_name: tenant } })
  return data
}

export async function unregisterEmailStream(tenant: string, email: string): Promise<{ message?: string }> {
  const { data } = await api.post<{ message?: string }>('/management/v1/email/unregister/stream', { email }, { params: { tenant_name: tenant } })
  return data
}

export interface EmailWatchStatus {
  email: string
  tenant_name: string
  has_access_token: boolean
  has_refresh_token: boolean
  profile?: { emailAddress?: string } | null
  watch?: { expiration_human?: string | null; is_expired?: boolean } | null
  errors?: string[]
}

export async function getEmailWatchStatus(tenant: string): Promise<EmailWatchStatus[]> {
  const { data } = await api.get<{ emails?: EmailWatchStatus[] }>('/management/v1/email/watch/all', { timeout: 60_000 })
  return (data.emails ?? []).filter((e) => e.tenant_name === tenant)
}
