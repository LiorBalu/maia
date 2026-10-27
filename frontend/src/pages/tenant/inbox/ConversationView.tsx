import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { ArrowRight, CircleStop, Phone, UserRound } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import {
  conversationTitle,
  endConversation,
  getConversation,
  markConversationRead,
  sendManagerMessage,
  type ApiConversation,
  type ApiMessage,
  type ReplyMode,
} from '@/api/conversations'
import { setCustomerReplyMode } from '@/api/customers'
import { Avatar } from '@/components/Avatar'
import { ErrorState } from '@/components/States'
import { dayKey, formatDayLabel, parseApiDate } from '@/lib/dates'

import { Composer } from './Composer'
import { CustomerDrawer } from './CustomerDrawer'
import { MessageBubble } from './MessageBubble'
import { platformIcon, platformKey, platformTone, type PlatformKey } from './platform'
import { ReplyModeSwitch } from './ReplyModeSwitch'

const VISIBLE_ROLES = new Set(['user', 'assistant', 'manager', 'agent'])

interface Props {
  tenant: string
  conversationId: string
  onBack: () => void
  /** Business-side sender for a channel (WAHA WhatsApp number, Gmail address); undefined when none is connected. */
  senderFor: (platform: PlatformKey) => string | undefined
}

export function ConversationView({ tenant, conversationId, onBack, senderFor }: Props) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [sendError, setSendError] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement | null>(null)

  const detailKey = ['conversation', tenant, conversationId] as const
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: detailKey,
    queryFn: () => getConversation(tenant, conversationId),
  })

  const messages = useMemo(() => {
    const list = (data?.messages ?? []).filter((m) => VISIBLE_ROLES.has(m.role))
    return [...list].sort((a, b) => (parseApiDate(a.date)?.getTime() ?? 0) - (parseApiDate(b.date)?.getTime() ?? 0))
  }, [data?.messages])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [messages.length, conversationId])

  // Opening a conversation marks it as read (list + detail caches follow).
  const readMutation = useMutation({
    mutationFn: () => markConversationRead(tenant, data!.phone_number, conversationId),
    onSuccess: () => {
      queryClient.setQueryData<ApiConversation>(detailKey, (old) => (old ? { ...old, read_by_manager: true } : old))
      patchListItem(queryClient, tenant, conversationId, { read_by_manager: true })
    },
  })
  const readRequested = useRef<string | null>(null)
  useEffect(() => {
    if (data && !data.read_by_manager && readRequested.current !== conversationId) {
      readRequested.current = conversationId
      readMutation.mutate()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data?.obj_id, data?.read_by_manager])

  const replyModeMutation = useMutation({
    mutationFn: (mode: ReplyMode) => setCustomerReplyMode(tenant, data!.phone_number, mode),
    onSuccess: (customer) => {
      queryClient.setQueryData<ApiConversation>(detailKey, (old) =>
        old ? { ...old, customer: { ...(old.customer ?? {}), ...customer } } : old,
      )
    },
  })

  const endMutation = useMutation({
    mutationFn: () => endConversation(tenant, data!.phone_number),
    onSuccess: () => {
      void refetch()
      void queryClient.invalidateQueries({ queryKey: ['conversations', tenant] })
    },
  })

  const sendMutation = useMutation({
    mutationFn: (text: string) =>
      sendManagerMessage(tenant, data!.phone_number, {
        message: text,
        platform: data!.platform,
        conversationId: data!.conversation_status === 'online' ? data!.obj_id : undefined,
        senderNumber: senderFor(platformKey(data!.platform)),
      }),
    onMutate: () => setSendError(null),
    onSuccess: () => {
      void refetch()
      void queryClient.invalidateQueries({ queryKey: ['conversations', tenant] })
    },
    onError: (error: unknown) => {
      const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
      const reason = typeof detail === 'string' ? detail : (error as Error)?.message || ''
      setSendError(t('inbox.compose.failed', { reason }))
    },
  })

  if (isLoading) return <ViewSkeleton />
  if (isError || !data)
    return (
      <div className="p-6">
        <ErrorState onRetry={() => void refetch()} />
      </div>
    )

  const key = platformKey(data.platform)
  const Icon = platformIcon[key]
  const title = conversationTitle(data)
  const online = data.conversation_status === 'online'
  const replyMode = data.customer?.reply_mode ?? 'AI'
  const isPhone = key === 'telephone'
  const groups = groupByDay(messages)

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex items-center gap-3 border-b border-brand-100 bg-white px-4 py-3">
        <button type="button" className="btn-ghost p-2 lg:hidden" onClick={onBack} aria-label={t('inbox.actions.back')}>
          <ArrowRight size={18} />
        </button>
        <Avatar name={title} />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold text-ink" dir="auto">
            {title}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-soft">
            <span dir="ltr">{data.phone_number}</span>
            <span className={clsx('badge ring-1', platformTone[key])}>
              <Icon size={11} />
              {t(`inbox.platforms.${key}`)}
            </span>
            <span className={clsx('badge', online ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-600')}>
              {t(`inbox.status.${online ? 'online' : 'closed'}`)}
            </span>
          </div>
        </div>
        <ReplyModeSwitch
          value={replyMode}
          onChange={(mode) => replyModeMutation.mutate(mode)}
          saving={replyModeMutation.isPending}
          compact
        />
        {online && (
          <button
            type="button"
            className="btn-ghost hidden p-2 md:inline-flex"
            title={t('inbox.actions.end')}
            aria-label={t('inbox.actions.end')}
            disabled={endMutation.isPending}
            onClick={() => {
              if (window.confirm(t('inbox.actions.endConfirm'))) endMutation.mutate()
            }}
          >
            <CircleStop size={18} />
          </button>
        )}
        <button
          type="button"
          className="btn-ghost p-2"
          title={t('inbox.actions.customer')}
          aria-label={t('inbox.actions.customer')}
          onClick={() => setDrawerOpen(true)}
        >
          <UserRound size={18} />
        </button>
      </header>

      <div className="flex-1 space-y-4 overflow-y-auto bg-surface px-4 py-4">
        {isPhone && (
          <div className="mx-auto flex w-fit items-center gap-2 rounded-full bg-violet-50 px-3 py-1 text-xs text-violet-700 ring-1 ring-violet-200">
            <Phone size={12} />
            {t('inbox.msg.callTranscript')}
          </div>
        )}
        {groups.length === 0 && <p className="py-10 text-center text-sm text-ink-muted">{t('inbox.noMessages')}</p>}
        {groups.map((group) => (
          <div key={group.key} className="space-y-2">
            <div className="sticky top-0 z-10 flex justify-center">
              <span className="rounded-full bg-white px-3 py-0.5 text-[11px] text-ink-muted shadow-sm ring-1 ring-brand-100">
                {group.label}
              </span>
            </div>
            {group.messages.map((message) => (
              <MessageBubble key={message.id} message={message} />
            ))}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <Composer
        disabled={isPhone}
        disabledReason={t('inbox.compose.phoneDisabled')}
        hint={!online ? t('inbox.compose.closedHint') : replyMode === 'AI' ? t('inbox.compose.botActiveHint') : undefined}
        sending={sendMutation.isPending}
        error={sendError}
        onSend={async (text) => {
          await sendMutation.mutateAsync(text).catch(() => undefined)
        }}
      />

      <CustomerDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        conversation={data}
        onReplyMode={(mode) => replyModeMutation.mutate(mode)}
        savingReplyMode={replyModeMutation.isPending}
      />
    </div>
  )
}

function groupByDay(messages: ApiMessage[]): { key: string; label: string; messages: ApiMessage[] }[] {
  const groups: { key: string; label: string; messages: ApiMessage[] }[] = []
  for (const message of messages) {
    const date = parseApiDate(message.date)
    const key = date ? dayKey(date) : 'unknown'
    const last = groups[groups.length - 1]
    if (last && last.key === key) last.messages.push(message)
    else groups.push({ key, label: date ? formatDayLabel(date) : '', messages: [message] })
  }
  return groups
}

/** Updates one conversation inside every cached inbox page of the tenant. */
export function patchListItem(
  queryClient: ReturnType<typeof useQueryClient>,
  tenant: string,
  conversationId: string,
  patch: Partial<ApiConversation>,
) {
  type Page = { conversations: ApiConversation[] }
  type Data = { pages: Page[]; pageParams: unknown[] }
  queryClient.setQueriesData<Data>({ queryKey: ['conversations', tenant] }, (old) => {
    if (!old) return old
    return {
      ...old,
      pages: old.pages.map((page) => ({
        ...page,
        conversations: page.conversations.map((c) => (c.obj_id === conversationId ? { ...c, ...patch } : c)),
      })),
    }
  })
}

function ViewSkeleton() {
  return (
    <div className="flex h-full flex-col" aria-busy>
      <div className="flex items-center gap-3 border-b border-brand-100 px-4 py-3">
        <div className="h-10 w-10 animate-pulse rounded-full bg-brand-100" />
        <div className="h-3 w-40 animate-pulse rounded bg-brand-100" />
      </div>
      <div className="flex-1 space-y-3 bg-surface p-4">
        {[60, 40, 70, 30].map((w, i) => (
          <div key={i} className={clsx('flex', i % 2 ? 'justify-end' : 'justify-start')}>
            <div className="h-10 animate-pulse rounded-2xl bg-brand-100" style={{ width: `${w}%` }} />
          </div>
        ))}
      </div>
    </div>
  )
}
