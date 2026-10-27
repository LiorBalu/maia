import { Globe, Mail, MessageCircle, Phone, type LucideIcon } from 'lucide-react'

/** The four channels the UI distinguishes; WAHA WhatsApp ("waha") folds into WhatsApp. */
export type PlatformKey = 'whatsapp' | 'email' | 'telephone' | 'web'

export const PLATFORM_KEYS: PlatformKey[] = ['whatsapp', 'email', 'telephone', 'web']

export function platformKey(platform?: string | null): PlatformKey {
  switch (platform) {
    case 'whatsapp':
    case 'waha':
      return 'whatsapp'
    case 'email':
      return 'email'
    case 'telephone':
      return 'telephone'
    default:
      return 'web'
  }
}

export const platformIcon: Record<PlatformKey, LucideIcon> = {
  whatsapp: MessageCircle,
  email: Mail,
  telephone: Phone,
  web: Globe,
}

/** Badge colors per channel (WhatsApp green, email sky, phone violet, web grey). */
export const platformTone: Record<PlatformKey, string> = {
  whatsapp: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  email: 'bg-brand-50 text-brand-700 ring-brand-200',
  telephone: 'bg-violet-50 text-violet-700 ring-violet-200',
  web: 'bg-slate-100 text-slate-600 ring-slate-200',
}
