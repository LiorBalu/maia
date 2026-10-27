import { useEffect, useRef } from 'react'
import clsx from 'clsx'
import { useTranslation } from 'react-i18next'

import { conversationTitle, type ApiConversation, type ApiMessage } from '@/api/conversations'
import { Avatar } from '@/components/Avatar'
import { EmptyState, ErrorState } from '@/components/States'
import { formatListTime, parseApiDate } from '@/lib/dates'

import { platformIcon, platformKey, platformTone } from './platform'

interface Props {
  conversations: ApiConversation[]
  selectedId?: string
  onSelect: (conversation: ApiConversation) => void
  isLoading: boolean
  isError: boolean
  onRetry: () => void
  hasMore: boolean
  isFetchingMore: boolean
  onLoadMore: () => void
  filtered: boolean
}

export function ConversationList(props: Props) {
  const { t } = useTranslation()
  const { conversations, selectedId, onSelect, isLoading, isError, onRetry, hasMore, isFetchingMore, onLoadMore, filtered } = props
  const sentinelRef = useRef<HTMLLIElement | null>(null)

  // Load the next page when the end of the list scrolls into view.
  useEffect(() => {
    const node = sentinelRef.current
    if (!node || !hasMore) return
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting) && !isFetchingMore) onLoadMore()
    })
    observer.observe(node)
    return () => observer.disconnect()
  }, [hasMore, isFetchingMore, onLoadMore])

  if (isLoading) return <ListSkeleton />
  if (isError)
    return (
      <div className="p-4">
        <ErrorState onRetry={onRetry} />
      </div>
    )
  if (conversations.length === 0)
    return (
      <div className="p-4">
        <EmptyState text={filtered ? t('inbox.emptyFiltered') : t('inbox.empty')} />
      </div>
    )

  return (
    <ul className="divide-y divide-brand-50">
      {conversations.map((conversation) => (
        <li key={conversation.obj_id}>
          <ConversationItem
            conversation={conversation}
            active={conversation.obj_id === selectedId}
            onClick={() => onSelect(conversation)}
          />
        </li>
      ))}
      <li ref={sentinelRef} className="p-3 text-center text-xs text-ink-muted">
        {hasMore ? (
          <button type="button" className="btn-ghost text-xs" onClick={onLoadMore} disabled={isFetchingMore}>
            {isFetchingMore ? t('common.loading') : t('inbox.loadMore')}
          </button>
        ) : null}
      </li>
    </ul>
  )
}

function ConversationItem({
  conversation,
  active,
  onClick,
}: {
  conversation: ApiConversation
  active: boolean
  onClick: () => void
}) {
  const { t } = useTranslation()
  const key = platformKey(conversation.platform)
  const Icon = platformIcon[key]
  const title = conversationTitle(conversation)
  const last = conversation.messages?.[conversation.messages.length - 1]
  const when = parseApiDate(conversation.last_message_date ?? conversation.date)
  const unread = !conversation.read_by_manager
  const closed = conversation.conversation_status !== 'online'

  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        'flex w-full items-start gap-3 px-4 py-3 text-start transition-colors hover:bg-brand-50',
        active && 'bg-brand-100/70 hover:bg-brand-100/70',
      )}
      aria-current={active ? 'true' : undefined}
    >
      <div className="relative shrink-0">
        <Avatar name={title} />
        <span
          className={clsx(
            'absolute -bottom-1 -end-1 grid h-5 w-5 place-items-center rounded-full ring-2 ring-white',
            platformTone[key],
          )}
          title={t(`inbox.platforms.${key}`)}
        >
          <Icon size={11} />
        </span>
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <span className={clsx('truncate text-sm', unread ? 'font-semibold text-ink' : 'font-medium text-ink')} dir="auto">
            {title}
          </span>
          <span className="shrink-0 text-[11px] text-ink-muted">{when ? formatListTime(when) : ''}</span>
        </div>
        <div className="mt-0.5 flex items-center justify-between gap-2">
          <span className={clsx('line-clamp-1 text-xs', unread ? 'text-ink' : 'text-ink-soft')} dir="auto">
            {previewText(last, key, t)}
          </span>
          {unread ? (
            <span className="h-2 w-2 shrink-0 rounded-full bg-brand-500" aria-label={t('inbox.unread')} />
          ) : closed ? (
            <span className="shrink-0 text-[10px] text-ink-muted">{t('inbox.status.closed')}</span>
          ) : null}
        </div>
      </div>
    </button>
  )
}

function previewText(
  message: ApiMessage | undefined,
  platform: ReturnType<typeof platformKey>,
  t: (key: string) => string,
): string {
  if (!message) return platform === 'telephone' ? t('inbox.preview.call') : ''
  const who = message.role === 'assistant' ? `${t('inbox.roles.assistant')}: ` : message.role === 'manager' || message.role === 'agent' ? `${t('inbox.roles.manager')}: ` : ''
  const type = (message.msg_type || 'text').toLowerCase()
  const text = (message.message || '').replace(/\s+/g, ' ').trim()
  if (type === 'audio') return `${who}${t('inbox.preview.audio')}${text ? ` · ${text}` : ''}`
  if (type === 'image') return `${who}${t('inbox.preview.image')}`
  if (type === 'video') return `${who}${t('inbox.preview.video')}`
  if (!text && message.media_url) return `${who}${t('inbox.preview.file')}`
  if (!text && platform === 'telephone') return t('inbox.preview.call')
  return `${who}${text}`
}

function ListSkeleton() {
  return (
    <ul className="divide-y divide-brand-50" aria-busy>
      {Array.from({ length: 8 }, (_, i) => (
        <li key={i} className="flex items-center gap-3 px-4 py-3">
          <div className="h-10 w-10 animate-pulse rounded-full bg-brand-100" />
          <div className="flex-1">
            <div className="h-3 w-1/2 animate-pulse rounded bg-brand-100" />
            <div className="mt-2 h-2.5 w-3/4 animate-pulse rounded bg-brand-50" />
          </div>
        </li>
      ))}
    </ul>
  )
}
