import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { CopyField, Field, SectionCard } from '@/components/Form'
import { getPublicBackendUrl, setPublicBackendUrl, webhookUrl } from '@/lib/publicUrl'

/** Shows the webhook addresses to paste into Twilio, built from the public backend URL the user enters. */
export function WebhookCard({ title, subtitle, items }: { title: string; subtitle: string; items: { label: string; path: string; help?: string }[] }) {
  const { t } = useTranslation()
  const [base, setBase] = useState(getPublicBackendUrl())
  const [, bump] = useState(0)

  return (
    <SectionCard title={title} subtitle={subtitle}>
      <div className="space-y-3">
        <Field label={t('settings.webhooks.publicUrl')} help={t('settings.webhooks.publicUrlHelp')}>
          <input
            className="input text-left"
            dir="ltr"
            placeholder="https://xxxx.ngrok-free.app"
            value={base}
            onChange={(e) => setBase(e.target.value)}
            onBlur={() => {
              setPublicBackendUrl(base)
              bump((n) => n + 1)
            }}
          />
        </Field>
        {items.map((item) => (
          <CopyField key={item.path} label={item.label} value={webhookUrl(item.path)} help={item.help} />
        ))}
      </div>
    </SectionCard>
  )
}
