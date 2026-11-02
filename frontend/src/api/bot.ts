import { api } from './client'

const enc = encodeURIComponent

// ---------- Prompts ----------

export interface Prompt {
  id?: string
  name: string
  tenant_name: string
  /** The prompt text (the backend exposes it as both `prompt` and `content`). */
  prompt?: string | null
  content?: string | null
  label?: string | null
  modified_at?: string | null
  modified_by?: string | null
}

/** Prompt names the UI edits; everything else (RAG internals) stays hidden. */
export const CHAT_PROMPT = 'default'
export const PHONE_PROMPT = 'platform_realtime'

export async function listPrompts(tenant: string): Promise<Prompt[]> {
  const { data } = await api.get<Prompt[]>(`/management/v1/prompts/${enc(tenant)}`)
  return data
}

export async function updatePromptText(tenant: string, name: string, content: string): Promise<Prompt> {
  const { data } = await api.put<Prompt>(`/management/v1/prompts/${enc(tenant)}/${enc(name)}`, {
    name,
    tenant_name: tenant,
    content,
  })
  return data
}

export async function createPromptText(tenant: string, name: string, content: string): Promise<Prompt> {
  const { data } = await api.post<Prompt>('/management/v1/prompts', {
    name,
    tenant_name: tenant,
    content,
  })
  return data
}

// ---------- Agent (model settings) ----------

export interface Agent {
  id: string
  tenant_name: string
  name: string
  description: string
  is_active: boolean
  model: string
  provider: string
  score_threshold: number
  top_k_general: number
  top_k_qa: number
  direct_answer_threshold: number
  created_at?: number
  updated_at?: number
}

export type AgentInput = Omit<Agent, 'id' | 'tenant_name' | 'created_at' | 'updated_at'>

export async function listAgents(tenant: string): Promise<Agent[]> {
  const { data } = await api.get<Agent[]>('/management/v1/agents/', { params: { tenant_name: tenant } })
  return data
}

export async function updateAgent(agentId: string, input: AgentInput): Promise<Agent> {
  const { data } = await api.put<Agent>(`/management/v1/agents/${enc(agentId)}`, input)
  return data
}

export async function createAgent(tenant: string, input: AgentInput): Promise<Agent> {
  const { data } = await api.post<Agent>('/management/v1/agents/', input, { params: { tenant_name: tenant } })
  return data
}

// The backend RAG matches these exact values (conversational_rag.py: "openai" / "anthropic" / "google_llm").
export const PROVIDERS = ['openai', 'anthropic', 'google_llm'] as const

// OpenAI: the default (gpt-6.1-terra, also the backend's default) first, then the newest families
// (gpt-6.1 / gpt-6 / gpt-5.6 tiers: astra = strongest, sol = strong, terra = balanced, luna = cheapest),
// the dated gpt-5.x releases and the gpt-4 generation.
export const MODEL_SUGGESTIONS: Record<string, string[]> = {
  openai: [
    'gpt-6.1-terra',
    'gpt-6.1-sol',
    'gpt-6-astra',
    'gpt-6-sol',
    'gpt-6-luna',
    'gpt-5.6-sol',
    'gpt-5.6-terra',
    'gpt-5.6-luna',
    'gpt-5.5',
    'gpt-5.4',
    'gpt-5.4-mini',
    'gpt-5.4-nano',
    'gpt-5.2',
    'gpt-5.1',
    'gpt-4.1',
    'gpt-4.1-mini',
    'gpt-4.1-nano',
    'gpt-4o',
    'gpt-4o-mini',
  ],
  anthropic: ['claude-sonnet-4-5', 'claude-haiku-4-5', 'claude-opus-4-1'],
  google_llm: ['gemini-2.5-pro', 'gemini-2.5-flash'],
}

// ---------- Test chat (web channel, same pipeline as the real channels) ----------

export interface TestChatReply {
  message?: string | null
  tool_call?: unknown[]
}

/** Sends one message through the web channel and returns the bot's answer synchronously. */
export async function sendTestMessage(tenant: string, from: string, body: string): Promise<TestChatReply> {
  const form = new FormData()
  form.append('From', from)
  form.append('Body', body)
  form.append('ProfileName', 'בדיקה')
  const { data } = await api.post<TestChatReply>(`/api/async/web/${enc(tenant)}`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 120_000,
  })
  return data
}
