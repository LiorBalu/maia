import clsx from 'clsx'
import { Mail, MessageCircle, Phone } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { Channel } from '@/api/tenants'

export function StatusBadge({ status }: { status: string }) {
  const { t } = useTranslation()
  const tone =
    status === 'active'
      ? 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200'
      : status === 'inactive'
        ? 'bg-slate-100 text-slate-600 ring-1 ring-slate-200'
        : 'bg-amber-50 text-amber-700 ring-1 ring-amber-200'
  return <span className={clsx('badge', tone)}>{t(`tenants.status.${status}`, { defaultValue: status })}</span>
}

const channelIcons = { whatsapp: MessageCircle, email: Mail, telephone: Phone } as const

export function ChannelBadge({ channel }: { channel: Channel }) {
  const { t } = useTranslation()
  const Icon = channelIcons[channel]
  return (
    <span className="badge bg-brand-50 text-brand-800 ring-1 ring-brand-100">
      <Icon size={12} />
      {t(`tenants.channels.${channel}`)}
    </span>
  )
}

export function RoleBadge({ role }: { role: 'staff' | 'user' }) {
  const { t } = useTranslation()
  const tone = role === 'staff' ? 'bg-brand-600 text-white' : 'bg-slate-100 text-slate-700'
  return <span className={clsx('badge', tone)}>{t(`roles.${role}`)}</span>
}
