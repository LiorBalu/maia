import { useEffect, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import clsx from 'clsx'
import { RefreshCw, Wrench } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { sendTestMessage } from '@/api/bot'
import { endConversation } from '@/api/conversations'
import { InlineNotice } from '@/components/Form'
import { formatTime } from '@/lib/dates'
import { errorDetail } from '@/lib/errors'
import { Composer } from '@/pages/tenant/inbox/Composer'

interface TestMessage {
  role: 'user' | 'bot' | 'error'
  text: string
  tools?: unknown[]
  at: number
}

function storageKey(tenant: string, kind: 'id' | 'log') {
  return `maia_test_${kind}:${tenant}`
}

function loadTestId(tenant: string): string {
  try {
    const existing = localStorage.getItem(storageKey(tenant, 'id'))
    if (existing) return existing
    const fresh = `test-${Math.floor(100000 + Math.random() * 900000)}`
    localStorage.setItem(storageKey(tenant, 'id'), fresh)
    return fresh
  } catch {
    return 'test-browser'
  }
}

function loadLog(tenant: string): TestMessage[] {
  try {
    return JSON.parse(sessionStorage.getItem(storageKey(tenant, 'log')) || '[]') as TestMessage[]
  } catch {
    return []
  }
}

/** Talks to the bot through the web channel: same prompt, knowledge, tools and history as the real channels. */
export function TestChatTab({ tenant }: { tenant: string }) {
  const { t } = useTranslation()
  const [testId] = useState(() => loadTestId(tenant))
  const [log, setLog] = useState<TestMessage[]>(() => loadLog(tenant))
  const bottomRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    try {
      sessionStorage.setItem(storageKey(tenant, 'log'), JSON.stringify(log.slice(-100)))
    } catch {
      /* ignore */
    }
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [log, tenant])

  const send = useMutation({
    mutationFn: (text: string) => sendTestMessage(tenant, testId, text),
    onMutate: (text) => setLog((l) => [...l, { role: 'user', text, at: Date.now() }]),
    onSuccess: (reply) =>
      setLog((l) => [
        ...l,
        { role: 'bot', text: (reply.message ?? '').trim() || t('bot.test.emptyReply'), tools: reply.tool_call ?? [], at: Date.now() },
      ]),
    onError: (error) => setLog((l) => [...l, { role: 'error', text: t('bot.test.failed', { reason: errorDetail(error) }), at: Date.now() }]),
  })

  const reset = useMutation({
    mutationFn: () => endConversation(tenant, testId).catch(() => undefined),
    onSuccess: () => setLog([]),
  })

  return (
    <div className="space-y-3">
      <InlineNotice>
        {t('bot.test.notice')}{' '}
        <Link to={`/s/${encodeURIComponent(tenant)}/inbox?p=web`} className="text-brand-700 underline">
          {t('bot.test.openInbox')}
        </Link>
      </InlineNotice>
      <div className="card flex h-[calc(100vh-19rem)] min-h-[420px] flex-col overflow-hidden">
        <div className="flex items-center justify-between gap-3 border-b border-brand-100 px-4 py-2 text-xs text-ink-soft">
          <span>
            {t('bot.test.id')}: <span className="font-mono" dir="ltr">{testId}</span>
          </span>
          <button type="button" className="btn-ghost text-xs" onClick={() => reset.mutate()} disabled={reset.isPending}>
            <RefreshCw size={14} />
            {t('bot.test.reset')}
          </button>
        </div>
        <div className="flex-1 space-y-3 overflow-y-auto bg-surface px-4 py-4">
          {log.length === 0 && <p className="py-10 text-center text-sm text-ink-muted">{t('bot.test.empty')}</p>}
          {log.map((m, i) => (
            <div key={i} className={clsx('flex', m.role === 'user' ? 'justify-start' : 'justify-end')}>
              <div className={clsx('max-w-[85%] sm:max-w-[70%]', m.role === 'user' ? 'items-start' : 'items-end')}>
                <div
                  className={clsx(
                    'rounded-2xl px-3.5 py-2 text-sm shadow-sm',
                    m.role === 'user' && 'rounded-ss-md border border-brand-100 bg-white text-ink',
                    m.role === 'bot' && 'rounded-se-md bg-brand-100 text-ink',
                    m.role === 'error' && 'rounded-se-md bg-red-50 text-red-700 ring-1 ring-red-200',
                  )}
                >
                  <p className="whitespace-pre-wrap break-words" dir="auto">
                    {m.text}
                  </p>
                  {m.tools && m.tools.length > 0 && (
                    <details className="mt-2 text-xs text-ink-soft">
                      <summary className="flex cursor-pointer items-center gap-1">
                        <Wrench size={12} />
                        {t('bot.test.tools', { count: m.tools.length })}
                      </summary>
                      <pre className="mt-1 max-h-48 overflow-auto rounded-lg bg-white/70 p-2 text-left font-mono text-[11px]" dir="ltr">
                        {JSON.stringify(m.tools, null, 2)}
                      </pre>
                    </details>
                  )}
                </div>
                <div className="mt-0.5 px-1 text-[11px] text-ink-muted">{formatTime(new Date(m.at))}</div>
              </div>
            </div>
          ))}
          {send.isPending && (
            <div className="flex justify-end">
              <div className="rounded-2xl rounded-se-md bg-brand-100 px-3.5 py-2 text-sm text-ink-muted">{t('bot.test.thinking')}</div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
        <Composer
          sending={send.isPending}
          onSend={async (text) => {
            await send.mutateAsync(text).catch(() => undefined)
          }}
        />
      </div>
    </div>
  )
}
