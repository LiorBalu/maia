import clsx from 'clsx'
import { AlertCircle, Mic, Paperclip } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { ApiMessage } from '@/api/conversations'
import { formatTime, parseApiDate } from '@/lib/dates'

/**
 * One message. The customer sits on the start side (right in RTL), the business
 * (bot or human) on the end side. Voice notes play inline with their transcription.
 */
export function MessageBubble({ message }: { message: ApiMessage }) {
  const { t } = useTranslation()
  const role = message.role
  const fromCustomer = role === 'user'
  const fromHuman = role === 'manager' || role === 'agent'
  const date = parseApiDate(message.date)
  const type = (message.msg_type || 'text').toLowerCase()
  const media = message.media_url || undefined
  const text = (message.message || '').trim()
  const subject = typeof message.meta_data?.subject === 'string' ? (message.meta_data.subject as string) : undefined
  const failed = message.status === 'failed' || message.status === 'undelivered'

  return (
    <div className={clsx('flex', fromCustomer ? 'justify-start' : 'justify-end')}>
      <div className={clsx('flex max-w-[85%] flex-col sm:max-w-[70%]', fromCustomer ? 'items-start' : 'items-end')}>
        {!fromCustomer && (
          <span className="mb-0.5 px-1 text-[11px] text-ink-muted">
            {t(fromHuman ? 'inbox.roles.manager' : 'inbox.roles.assistant')}
          </span>
        )}
        <div
          className={clsx(
            'rounded-2xl px-3.5 py-2 text-sm shadow-sm',
            fromCustomer && 'rounded-ss-md border border-brand-100 bg-white text-ink',
            !fromCustomer && !fromHuman && 'rounded-se-md bg-brand-100 text-ink',
            fromHuman && 'rounded-se-md bg-brand-600 text-white',
          )}
        >
          {subject && (
            <div className="mb-1 text-xs font-semibold opacity-80" dir="auto">
              {t('inbox.msg.subject')}: {subject}
            </div>
          )}
          <MessageBody type={type} media={media} text={text} fromHuman={fromHuman} />
        </div>
        <div className="mt-0.5 flex items-center gap-1 px-1 text-[11px] text-ink-muted">
          {failed && (
            <span className="flex items-center gap-0.5 text-red-600">
              <AlertCircle size={11} />
              {t('inbox.msg.failed')}
            </span>
          )}
          {date && <span>{formatTime(date)}</span>}
        </div>
      </div>
    </div>
  )
}

function MessageBody({ type, media, text, fromHuman }: { type: string; media?: string; text: string; fromHuman: boolean }) {
  const { t } = useTranslation()
  const textNode = text ? (
    <p className="whitespace-pre-wrap break-words" dir="auto">
      {text}
    </p>
  ) : null

  if (type === 'audio' && media) {
    return (
      <div className="space-y-2">
        <audio controls preload="none" src={media} className="w-64 max-w-full" />
        {text && (
          <div>
            <div className={clsx('mb-0.5 flex items-center gap-1 text-[11px]', fromHuman ? 'text-white/80' : 'text-ink-muted')}>
              <Mic size={11} />
              {t('inbox.msg.transcript')}
            </div>
            {textNode}
          </div>
        )}
      </div>
    )
  }
  if (type === 'image' && media) {
    return (
      <div className="space-y-2">
        <a href={media} target="_blank" rel="noreferrer">
          <img src={media} alt="" className="max-h-64 rounded-xl" loading="lazy" />
        </a>
        {textNode}
      </div>
    )
  }
  if (type === 'video' && media) {
    return (
      <div className="space-y-2">
        <video controls preload="metadata" src={media} className="max-h-64 rounded-xl" />
        {textNode}
      </div>
    )
  }
  if (media) {
    return (
      <div className="space-y-2">
        <a
          href={media}
          target="_blank"
          rel="noreferrer"
          className={clsx('inline-flex items-center gap-1 underline', fromHuman ? 'text-white' : 'text-brand-700')}
        >
          <Paperclip size={13} />
          {t('inbox.msg.openFile')}
        </a>
        {textNode}
      </div>
    )
  }
  return textNode ?? <p className="italic opacity-60">{t('inbox.msg.empty')}</p>
}
