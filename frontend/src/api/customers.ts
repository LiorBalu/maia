import { api } from './client'
import type { ApiCustomer, ReplyMode } from './conversations'

const enc = encodeURIComponent

/** Who answers this customer: the bot ("AI") or a human ("agent"). */
export async function setCustomerReplyMode(tenant: string, phoneNumber: string, replyMode: ReplyMode): Promise<ApiCustomer> {
  const { data } = await api.put<ApiCustomer>(`/management/v1/customers/${enc(tenant)}/${enc(phoneNumber)}/reply_mode`, {
    reply_mode: replyMode,
  })
  return data
}
