import clsx from 'clsx'
import { CalendarDays, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { conversationTitle, type ApiConversation, type ReplyMode } from '@/api/conversations'
import { Avatar } from '@/components/Avatar'
import { formatDateTime, parseApiDate } from '@/lib/dates'

import { platformIcon, platformKey, platformTone } from './platform'
import { ReplyModeSwitch } from './ReplyModeSwitch'

interface Props {
  open: boolean
  onClose: () => void
  conversation: ApiConversation
  onReplyMode: (mode: ReplyMode) => void
  savingReplyMode: boolean
}

/** Side panel with the customer's details; replaces a separate customers screen. */
export function CustomerDrawer({ open, onClose, conversation, onReplyMode, savingReplyMode }: Props) {
  const { t } = useTranslation()
  if (!open) return null
  const customer = conversation.customer ?? {}
  const title = conversationTitle(conversation)
  const key = platformKey(conversation.platform)
  const Icon = platformIcon[key]
  const created = parseApiDate(customer.created_date)
  const started = parseApiDate(conversation.date)
  const last = parseApiDate(conversation.last_message_date)
  const meetings = customer.meetings ?? []

  return (
    <div className="fixed inset-0 z-40">
      <div className="absolute inset-0 bg-ink/30" onClick={onClose} />
      <aside className="absolute inset-y-0 end-0 flex w-full max-w-sm flex-col bg-white shadow-pop" aria-label={t('inbox.customer.title')}>
        <div className="flex items-center justify-between border-b border-brand-100 px-5 py-4">
          <h2 className="text-base font-semibold text-ink">{t('inbox.customer.title')}</h2>
          <button type="button" className="btn-ghost p-2" onClick={onClose} aria-label={t('common.close')}>
            <X size={18} />
          </button>
        </div>
        <div className="flex-1 space-y-6 overflow-y-auto px-5 py-5">
          <div className="flex items-center gap-3">
            <Avatar name={title} size="lg" />
            <div className="min-w-0">
              <div className="truncate text-lg font-semibold text-ink" dir="auto">
                {title}
              </div>
              <span className={clsx('badge ring-1', platformTone[key])}>
                <Icon size={12} />
                {t(`inbox.platforms.${key}`)}
              </span>
            </div>
          </div>

          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
            <Row label={t('inbox.customer.phone')}>
              <span dir="ltr" className="break-all">
                {customer.phone_number || conversation.phone_number}
              </span>
            </Row>
            {customer.email && (
              <Row label={t('inbox.customer.email')}>
                <span dir="ltr" className="break-all">
                  {customer.email}
                </span>
              </Row>
            )}
            {customer.status && <Row label={t('inbox.customer.status')}>{customer.status}</Row>}
            {created && <Row label={t('inbox.customer.created')}>{formatDateTime(created)}</Row>}
          </dl>

          <section>
            <h3 className="label">{t('inbox.replyMode.label')}</h3>
            <ReplyModeSwitch value={customer.reply_mode ?? 'AI'} onChange={onReplyMode} saving={savingReplyMode} />
            <p className="mt-1 text-xs text-ink-muted">{t('inbox.replyMode.help')}</p>
          </section>

          <section>
            <h3 className="label">{t('inbox.customer.conversation')}</h3>
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
              <Row label={t('inbox.customer.status')}>
                {t(`inbox.status.${conversation.conversation_status === 'online' ? 'online' : 'closed'}`)}
              </Row>
              {started && <Row label={t('inbox.customer.started')}>{formatDateTime(started)}</Row>}
              {last && <Row label={t('inbox.customer.lastMessage')}>{formatDateTime(last)}</Row>}
            </dl>
          </section>

          <section>
            <h3 className="label flex items-center gap-1">
              <CalendarDays size={14} />
              {t('inbox.customer.meetings')}
            </h3>
            {meetings.length === 0 ? (
              <p className="text-sm text-ink-muted">{t('inbox.customer.noMeetings')}</p>
            ) : (
              <ul className="space-y-2">
                {meetings.map((meeting, index) => {
                  const start = parseApiDate(meeting.start_time)
                  const end = parseApiDate(meeting.end_time)
                  return (
                    <li key={meeting.meeting_id ?? index} className="rounded-xl bg-brand-50 px-3 py-2 text-sm">
                      <div className="font-medium text-ink">{start ? formatDateTime(start) : '?'}</div>
                      {end && <div className="text-xs text-ink-soft">{t('inbox.customer.until')} {formatDateTime(end)}</div>}
                      {typeof meeting.status === 'string' && meeting.status && (
                        <div className="text-xs text-ink-muted">{meeting.status}</div>
                      )}
                    </li>
                  )
                })}
              </ul>
            )}
          </section>
        </div>
      </aside>
    </div>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-ink-muted">{label}</dt>
      <dd className="text-ink">{children}</dd>
    </>
  )
}
