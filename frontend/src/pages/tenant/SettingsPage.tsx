import { Table, Mail, MessageCircle, Phone, SlidersHorizontal } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useParams, useSearchParams } from 'react-router-dom'

import { PageHeader } from '@/components/PageHeader'
import { Tabs } from '@/components/Tabs'

import { EmailTab } from './settings/EmailTab'
import { GeneralTab } from './settings/GeneralTab'
import { PhoneTab } from './settings/PhoneTab'
import { SheetsTab } from './settings/SheetsTab'
import { WhatsAppTab } from './settings/WhatsAppTab'

type TabKey = 'general' | 'whatsapp' | 'email' | 'phone' | 'sheets'
const KEYS: TabKey[] = ['general', 'whatsapp', 'email', 'phone', 'sheets']

export function SettingsPage() {
  const { t } = useTranslation()
  const { tenantName = '' } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const raw = searchParams.get('tab') as TabKey | null
  const tab: TabKey = raw && KEYS.includes(raw) ? raw : 'general'

  return (
    <>
      <PageHeader title={t('pages.settings.title')} subtitle={t('pages.settings.desc')} />
      <Tabs<TabKey>
        value={tab}
        onChange={(key) => setSearchParams(key === 'general' ? {} : { tab: key }, { replace: true })}
        tabs={[
          { key: 'general', label: t('settings.tabs.general'), icon: SlidersHorizontal },
          { key: 'whatsapp', label: t('settings.tabs.whatsapp'), icon: MessageCircle },
          { key: 'email', label: t('settings.tabs.email'), icon: Mail },
          { key: 'phone', label: t('settings.tabs.phone'), icon: Phone },
          { key: 'sheets', label: t('settings.tabs.sheets'), icon: Table },
        ]}
      />
      {tab === 'general' && <GeneralTab tenant={tenantName} />}
      {tab === 'whatsapp' && <WhatsAppTab tenant={tenantName} />}
      {tab === 'email' && <EmailTab tenant={tenantName} />}
      {tab === 'phone' && <PhoneTab tenant={tenantName} />}
      {tab === 'sheets' && <SheetsTab tenant={tenantName} />}
    </>
  )
}
