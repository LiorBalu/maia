import { api } from './client'

/** One `@gpt_tool` function registered in the backend (GET /management/v1/tools). */
export interface ToolInfo {
  name: string
  /** File the tool lives in: `google_calendar`, `google_sheets`, or a tenant module. */
  module: string
  /** First line of the docstring, in English (the full docstring is what the LLM reads). */
  description: string
}

/** The tools a business can give its bot; the chosen names are saved in the tenant's `tools` field. */
export async function listTools(): Promise<ToolInfo[]> {
  const { data } = await api.get<ToolInfo[]>('/management/v1/tools')
  return data
}
