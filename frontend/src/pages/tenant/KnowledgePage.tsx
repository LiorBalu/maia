import { useQuery } from '@tanstack/react-query'
import { FileText, MessageSquareText } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useParams, useSearchParams } from 'react-router-dom'

import { getQACount, listDocuments } from '@/api/knowledge'
import { PageHeader } from '@/components/PageHeader'
import { Tabs } from '@/components/Tabs'

import { DocumentsTab } from './knowledge/DocumentsTab'
import { QATab } from './knowledge/QATab'

type TabKey = 'docs' | 'qa'

export function KnowledgePage() {
  const { t } = useTranslation()
  const { tenantName = '' } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const tab: TabKey = searchParams.get('tab') === 'qa' ? 'qa' : 'docs'

  const docs = useQuery({ queryKey: ['documents', tenantName], queryFn: () => listDocuments(tenantName), enabled: Boolean(tenantName) })
  const qaCount = useQuery({ queryKey: ['qa-count', tenantName], queryFn: () => getQACount(tenantName), enabled: Boolean(tenantName) })

  return (
    <>
      <PageHeader title={t('pages.knowledge.title')} subtitle={t('pages.knowledge.desc')} />
      <Tabs<TabKey>
        value={tab}
        onChange={(key) => setSearchParams(key === 'docs' ? {} : { tab: key }, { replace: true })}
        tabs={[
          { key: 'docs', label: t('knowledge.tabs.docs'), icon: FileText, badge: docs.data?.length },
          { key: 'qa', label: t('knowledge.tabs.qa'), icon: MessageSquareText, badge: qaCount.data?.total_count },
        ]}
      />
      {tab === 'docs' ? <DocumentsTab tenant={tenantName} /> : <QATab tenant={tenantName} />}
    </>
  )
}
