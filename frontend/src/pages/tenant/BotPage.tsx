import { Bot, FlaskConical, ScrollText, Wrench } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useParams, useSearchParams } from 'react-router-dom'

import { PageHeader } from '@/components/PageHeader'
import { Tabs } from '@/components/Tabs'

import { ModelTab } from './bot/ModelTab'
import { PromptsTab } from './bot/PromptsTab'
import { TestChatTab } from './bot/TestChatTab'
import { ToolsTab } from './bot/ToolsTab'

type TabKey = 'prompt' | 'model' | 'tools' | 'test'

export function BotPage() {
  const { t } = useTranslation()
  const { tenantName = '' } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const raw = searchParams.get('tab')
  const tab: TabKey = raw === 'model' || raw === 'tools' || raw === 'test' ? raw : 'prompt'

  return (
    <>
      <PageHeader title={t('pages.bot.title')} subtitle={t('pages.bot.desc')} />
      <Tabs<TabKey>
        value={tab}
        onChange={(key) => setSearchParams(key === 'prompt' ? {} : { tab: key }, { replace: true })}
        tabs={[
          { key: 'prompt', label: t('bot.tabs.prompt'), icon: ScrollText },
          { key: 'model', label: t('bot.tabs.model'), icon: Bot },
          { key: 'tools', label: t('bot.tabs.tools'), icon: Wrench },
          { key: 'test', label: t('bot.tabs.test'), icon: FlaskConical },
        ]}
      />
      {tab === 'prompt' && <PromptsTab tenant={tenantName} />}
      {tab === 'model' && <ModelTab tenant={tenantName} />}
      {tab === 'tools' && <ToolsTab tenant={tenantName} />}
      {tab === 'test' && <TestChatTab tenant={tenantName} />}
    </>
  )
}
