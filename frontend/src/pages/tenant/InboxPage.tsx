import { useCallback, useMemo, useState } from 'react'
import { useInfiniteQuery, useQuery, useQueryClient, type InfiniteData, type QueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { Inbox, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useParams, useSearchParams } from 'react-router-dom'

import { listConversations, type ApiConversation, type ConversationPage } from '@/api/conversations'
import { getTenant } from '@/api/tenants'
import { PageHeader } from '@/components/PageHeader'
import { useConversationStream, type StreamStatus } from '@/hooks/useConversationStream'
import { useDebounced } from '@/hooks/useDebounced'

import { ConversationList } from './inbox/ConversationList'
import { ConversationView } from './inbox/ConversationView'
import { PLATFORM_KEYS, platformIcon, platformKey, type PlatformKey } from './inbox/platform'

export function InboxPage() {
  const { t } = useTranslation()
  const { tenantName = '' } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()

  const selectedId = searchParams.get('c') || undefined
  const platformParam = searchParams.get('p')
  const platform = PLATFORM_KEYS.includes(platformParam as PlatformKey) ? (platformParam as PlatformKey) : undefined
  const [search, setSearch] = useState('')
  const query = useDebounced(search.trim(), 350)

  const listQuery = useInfiniteQuery({
    queryKey: ['conversations', tenantName, platform, query] as const,
    queryFn: ({ pageParam }) => listConversations(tenantName, { cursor: pageParam, platform, query }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => (last.has_more && last.next_cursor ? last.next_cursor : undefined),
    enabled: Boolean(tenantName),
  })

  const tenantQuery = useQuery({
    queryKey: ['tenant', tenantName],
    queryFn: () => getTenant(tenantName),
    staleTime: 5 * 60_000,
    enabled: Boolean(tenantName),
  })

  const onLive = useCallback(
    (conversation: ApiConversation) => applyLiveUpdate(queryClient, tenantName, conversation),
    [queryClient, tenantName],
  )
  const streamStatus = useConversationStream(tenantName || undefined, onLive)

  const conversations = useMemo(() => listQuery.data?.pages.flatMap((p) => p.conversations) ?? [], [listQuery.data])
  const total = listQuery.data?.pages[0]?.total_count

  const senderFor = useCallback(
    (key: PlatformKey): string | undefined => {
      const tenant = tenantQuery.data
      if (key === 'whatsapp') return tenant?.waha_accounts?.[0]?.phone_number || undefined
      return undefined
    },
    [tenantQuery.data],
  )

  function updateParams(patch: Record<string, string | undefined>) {
    const next = new URLSearchParams(searchParams)
    for (const [k, v] of Object.entries(patch)) {
      if (v) next.set(k, v)
      else next.delete(k)
    }
    setSearchParams(next, { replace: true })
  }

  return (
    <>
      <PageHeader
        title={t('pages.inbox.title')}
        subtitle={t('pages.inbox.desc')}
        actions={
          <div className="flex items-center gap-2">
            {typeof total === 'number' && (
              <span className="badge bg-brand-100 text-brand-800">{t('inbox.count', { count: total })}</span>
            )}
            <LiveBadge status={streamStatus} />
          </div>
        }
      />

      <div className="grid h-[calc(100vh-11.5rem)] min-h-[520px] overflow-hidden rounded-2xl border border-brand-100 bg-white shadow-card lg:grid-cols-[22rem_minmax(0,1fr)]">
        <section className={clsx('flex min-h-0 flex-col border-e border-brand-100', selectedId && 'hidden lg:flex')}>
          <div className="space-y-2 border-b border-brand-100 p-3">
            <div className="relative">
              <Search size={15} className="pointer-events-none absolute inset-y-0 start-3 my-auto text-ink-muted" />
              <input
                type="search"
                className="input ps-9"
                placeholder={t('inbox.search')}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            <div className="flex flex-wrap gap-1.5">
              <FilterChip active={!platform} onClick={() => updateParams({ p: undefined })}>
                {t('inbox.filters.all')}
              </FilterChip>
              {PLATFORM_KEYS.map((key) => {
                const Icon = platformIcon[key]
                return (
                  <FilterChip key={key} active={platform === key} onClick={() => updateParams({ p: key })}>
                    <Icon size={12} />
                    {t(`inbox.platforms.${key}`)}
                  </FilterChip>
                )
              })}
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            <ConversationList
              conversations={conversations}
              selectedId={selectedId}
              onSelect={(c) => updateParams({ c: c.obj_id })}
              isLoading={listQuery.isLoading}
              isError={listQuery.isError}
              onRetry={() => void listQuery.refetch()}
              hasMore={Boolean(listQuery.hasNextPage)}
              isFetchingMore={listQuery.isFetchingNextPage}
              onLoadMore={() => void listQuery.fetchNextPage()}
              filtered={Boolean(platform || query)}
            />
          </div>
        </section>

        <section className={clsx('flex min-h-0 min-w-0 flex-col', !selectedId && 'hidden lg:flex')}>
          {selectedId ? (
            <ConversationView
              key={selectedId}
              tenant={tenantName}
              conversationId={selectedId}
              onBack={() => updateParams({ c: undefined })}
              senderFor={senderFor}
            />
          ) : (
            <div className="flex h-full flex-col items-center justify-center gap-3 bg-surface p-8 text-center">
              <div className="grid h-14 w-14 place-items-center rounded-2xl bg-brand-100 text-brand-700">
                <Inbox size={28} />
              </div>
              <p className="text-sm font-medium text-ink">{t('inbox.select')}</p>
              <p className="text-xs text-ink-muted">{t('inbox.selectHint')}</p>
            </div>
          )}
        </section>
      </div>
    </>
  )
}

function FilterChip({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        'inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium transition-colors',
        active ? 'bg-brand-600 text-white' : 'bg-brand-50 text-ink-soft hover:bg-brand-100 hover:text-brand-800',
      )}
    >
      {children}
    </button>
  )
}

function LiveBadge({ status }: { status: StreamStatus }) {
  const { t } = useTranslation()
  const tone = status === 'open' ? 'bg-emerald-500' : status === 'connecting' ? 'bg-amber-400' : 'bg-red-500'
  const label = status === 'open' ? t('inbox.live') : status === 'connecting' ? t('inbox.connecting') : t('inbox.offline')
  return (
    <span className="badge bg-white text-ink-soft ring-1 ring-brand-100" title={label}>
      <span className={clsx('h-2 w-2 rounded-full', tone, status === 'open' && 'animate-pulse')} />
      <span className="hidden sm:inline">{label}</span>
    </span>
  )
}

/**
 * A live event carries the whole conversation. Move it to the top of every cached
 * list of this tenant (inserting it when it is new and matches that list's platform
 * filter), and refresh the open conversation's messages in place.
 */
function applyLiveUpdate(queryClient: QueryClient, tenant: string, conversation: ApiConversation) {
  const key = platformKey(conversation.platform)
  const preview: ApiConversation = { ...conversation, messages: (conversation.messages ?? []).slice(-1) }

  for (const cached of queryClient.getQueryCache().findAll({ queryKey: ['conversations', tenant] })) {
    const [, , filterPlatform, filterQuery] = cached.queryKey as readonly [string, string, string | undefined, string | undefined]
    const allowInsert = !filterQuery && (!filterPlatform || filterPlatform === key)
    queryClient.setQueryData<InfiniteData<ConversationPage, string | undefined>>(cached.queryKey, (old) => {
      if (!old || old.pages.length === 0) return old
      let found = false
      const pages = old.pages.map((page) => {
        if (!page.conversations.some((c) => c.obj_id === conversation.obj_id)) return page
        found = true
        return { ...page, conversations: page.conversations.filter((c) => c.obj_id !== conversation.obj_id) }
      })
      if (!found && !allowInsert) return old
      const first = pages[0]
      pages[0] = {
        ...first,
        conversations: [preview, ...first.conversations],
        total_count: found ? first.total_count : first.total_count + 1,
      }
      return { ...old, pages }
    })
  }

  queryClient.setQueryData<ApiConversation>(['conversation', tenant, conversation.obj_id], (old) =>
    old ? { ...old, ...conversation } : old,
  )
}
