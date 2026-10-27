import { api } from './client'

const enc = encodeURIComponent

// ---------- Documents (files / URLs indexed into the RAG) ----------

export interface KnowledgeDocument {
  id: string
  tenant_name: string
  title: string
  url: string
  total_parts: number
  is_indexed: boolean
  created_at: string
  updated_at: string
  is_protected?: boolean
  categories?: string[]
}

export interface DocumentProgress {
  document_id: string
  total_parts: number
  completed_parts: number
  progress_percentage: number
  is_indexed: boolean
  status: string
}

export interface DocumentPart {
  id: string
  part_number: string | number
  content: string
  vector_id?: string | null
}

export const DOCUMENT_ACCEPT = '.pdf,.doc,.docx,.txt,.xls,.xlsx,.csv'

export async function listDocuments(tenant: string): Promise<KnowledgeDocument[]> {
  const { data } = await api.get<KnowledgeDocument[]>('/management/v1/embedded_documents/', { params: { tenant_name: tenant } })
  return data
}

export async function uploadDocument(tenant: string, file: File, title?: string): Promise<KnowledgeDocument> {
  const form = new FormData()
  form.append('tenant_name', tenant)
  form.append('file', file, file.name)
  form.append('title', title?.trim() || file.name)
  const { data } = await api.post<KnowledgeDocument>('/management/v1/embedded_documents/', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return data
}

export async function addUrlDocument(tenant: string, url: string, maxDepth = 1): Promise<KnowledgeDocument> {
  const form = new FormData()
  form.append('tenant_name', tenant)
  form.append('url', url.trim())
  form.append('max_depth', String(maxDepth))
  form.append('max_pages', maxDepth > 1 ? '50' : '1')
  form.append('same_domain_only', 'true')
  const { data } = await api.post<KnowledgeDocument>('/management/v1/embedded_documents/scrape_url', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return data
}

export async function getDocumentProgress(documentId: string): Promise<DocumentProgress> {
  const { data } = await api.get<DocumentProgress>(`/management/v1/embedded_documents/${enc(documentId)}/progress`)
  return data
}

export async function deleteDocument(documentId: string): Promise<void> {
  await api.delete(`/management/v1/embedded_documents/${enc(documentId)}`)
}

export async function listDocumentParts(documentId: string, page = 1, pageSize = 20): Promise<DocumentPart[]> {
  const { data } = await api.get<DocumentPart[]>('/management/v1/embedded_parts/', {
    params: { document_id: documentId, page, page_size: pageSize },
  })
  return data
}

// ---------- Q&A bank ----------

export interface QAItem {
  id: string
  tenant_name: string
  question: string
  answer: string
  category: string
  is_indexed: boolean
  created_at: string
  updated_at: string
}

export interface QACount {
  tenant_name: string
  total_count: number
  indexed_count: number
  pending_count: number
}

export interface QACategory {
  category: string
  total_count: number
  indexed_count?: number
  pending_count?: number
}

export interface QAInput {
  question: string
  answer: string
  category?: string
}

export const QA_PAGE_SIZE = 50

export async function listQA(tenant: string, page = 1, category?: string): Promise<QAItem[]> {
  const { data } = await api.get<QAItem[]>('/management/v1/qa/', {
    params: { tenant_name: tenant, page, page_size: QA_PAGE_SIZE, category: category || undefined },
  })
  return data
}

export async function getQACount(tenant: string): Promise<QACount> {
  const { data } = await api.get<QACount>('/management/v1/qa/count', { params: { tenant_name: tenant } })
  return data
}

export async function getQACategories(tenant: string): Promise<QACategory[]> {
  const { data } = await api.get<QACategory[]>('/management/v1/qa/categories', { params: { tenant_name: tenant } })
  return data
}

export async function createQA(tenant: string, input: QAInput): Promise<QAItem> {
  const { data } = await api.post<QAItem>(
    '/management/v1/qa/',
    { ...input, category: input.category?.trim() || undefined, tenant_name: tenant },
    { params: { tenant_name: tenant } },
  )
  return data
}

export async function createQABulk(tenant: string, items: QAInput[]): Promise<{ created?: number; [key: string]: unknown }> {
  const { data } = await api.post('/management/v1/qa/bulk', items.map((i) => ({ ...i, tenant_name: tenant })), {
    params: { tenant_name: tenant },
  })
  return data
}

export async function updateQA(qaId: string, patch: Partial<QAInput>): Promise<QAItem> {
  const { data } = await api.put<QAItem>(`/management/v1/qa/${enc(qaId)}`, patch)
  return data
}

export async function deleteQA(qaId: string): Promise<void> {
  await api.delete(`/management/v1/qa/${enc(qaId)}`)
}
