import { api } from './client'

/** Platform values the backend stores on conversations and messages. */
export type Platform = 'whatsapp' | 'email' | 'telephone' | 'web' | 'waha' | (string & {})

export type MessageRole = 'user' | 'assistant' | 'manager' | 'agent' | 'tool' | 'tool_calls' | (string & {})

export interface ApiMessage {
  id: string
  message: string | null
  role: MessageRole
  msg_type?: string | null
  media_url?: string | null
  date?: string | null
  platform?: Platform
  profile_name?: string | null
  status?: string | null
  conversation_id?: string
  meta_data?: Record<string, unknown> | null
}

export interface ApiMeeting {
  meeting_id?: string
  start_time?: string | null
  end_time?: string | null
  meeting_url?: string | null
  status?: string | null
  [key: string]: unknown
}

export type ReplyMode = 'AI' | 'agent' | 'blocked' | (string & {})

export interface ApiCustomer {
  id?: string
  name?: string | null
  phone_number?: string
  email?: string | null
  status?: string | null
  reply_mode?: ReplyMode
  avatar?: string | null
  created_date?: string | null
  updated_date?: string | null
  notes?: unknown[]
  meetings?: ApiMeeting[]
  meta_data?: Record<string, unknown> | null
}

export interface ApiConversation {
  obj_id: string
  tenant_name?: string
  platform: Platform
  phone_number: string
  profile_name?: string | null
  conversation_status: string
  read_by_manager: boolean
  date?: string | null
  last_message_date?: string | null
  messages: ApiMessage[]
  customer?: ApiCustomer | null
  summaries?: unknown[]
  tags?: unknown[]
  meta_data?: Record<string, unknown> | null
}

export interface ConversationPage {
  conversations: ApiConversation[]
  next_cursor: string | null
  has_more: boolean
  total_count: number
  page_count: number
}

const enc = encodeURIComponent

export interface ListConversationsOptions {
  cursor?: string
  platform?: string
  query?: string
  limit?: number
}

/** One page of the inbox list; every item carries only its last message. */
export async function listConversations(tenant: string, opts: ListConversationsOptions = {}): Promise<ConversationPage> {
  const { data } = await api.get<ConversationPage>(`/management/v1/conversations/${enc(tenant)}/infinite`, {
    params: {
      include_messages: false,
      include_customer: true,
      max_result: opts.limit ?? 20,
      cursor: opts.cursor || undefined,
      platform: opts.platform || undefined,
      query: opts.query || undefined,
    },
  })
  return data
}

/** A single conversation with all its messages and the customer. */
export async function getConversation(tenant: string, conversationId: string): Promise<ApiConversation> {
  const { data } = await api.get<ApiConversation>(`/management/v1/conversations/${enc(tenant)}/${enc(conversationId)}`, {
    params: { include_tool_calls: false, include_media_url: true },
  })
  return data
}

export async function markConversationRead(tenant: string, phoneNumber: string, conversationId: string): Promise<void> {
  await api.put(`/management/v1/conversations/${enc(tenant)}/${enc(phoneNumber)}/${enc(conversationId)}/readed`)
}

/** Closes the customer's current online conversation; the next message opens a new one. */
export async function endConversation(tenant: string, phoneNumber: string): Promise<void> {
  await api.put(`/management/v1/conversations/${enc(tenant)}/${enc(phoneNumber)}/end-current-conversation`)
}

export interface SendManagerMessageInput {
  message: string
  platform: Platform
  conversationId?: string
  /** The business side of the channel: the WAHA number for WhatsApp, Gmail address for email. */
  senderNumber?: string | null
}

/** A human reply sent on behalf of the business (stored with role "manager"). */
export async function sendManagerMessage(tenant: string, phoneNumber: string, input: SendManagerMessageInput): Promise<void> {
  const sender = input.senderNumber || 'manager'
  await api.post(
    `/management/v1/messages/${enc(tenant)}/${enc(phoneNumber)}/${enc(sender)}`,
    { message: input.message, conversation_id: input.conversationId },
    { params: { platform: input.platform, role: 'manager' } },
  )
}

/** Name to show for a conversation: customer name, then WhatsApp profile name, then the number. */
export function conversationTitle(conversation: ApiConversation): string {
  return conversation.customer?.name?.trim() || conversation.profile_name?.trim() || conversation.phone_number
}
