import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RotateCcw } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { CHAT_PROMPT, PHONE_PROMPT, createPromptText, listPrompts, updatePromptText, type Prompt } from '@/api/bot'
import { InlineError, SaveButton, SectionCard, useSavedFlash } from '@/components/Form'
import { ErrorState } from '@/components/States'
import { formatDateTime, parseApiDate } from '@/lib/dates'
import { errorDetail } from '@/lib/errors'

export function PromptsTab({ tenant }: { tenant: string }) {
  const { t } = useTranslation()
  const prompts = useQuery({ queryKey: ['prompts', tenant], queryFn: () => listPrompts(tenant) })

  if (prompts.isLoading) return <div className="card p-6 text-sm text-ink-muted">{t('common.loading')}</div>
  if (prompts.isError) return <ErrorState onRetry={() => void prompts.refetch()} />

  const byName = (name: string) => prompts.data?.find((p) => p.name === name)
  return (
    <div className="grid gap-4 2xl:grid-cols-2">
      <PromptEditor
        tenant={tenant}
        name={CHAT_PROMPT}
        title={t('bot.prompt.chatTitle')}
        subtitle={t('bot.prompt.chatSubtitle')}
        prompt={byName(CHAT_PROMPT)}
      />
      <PromptEditor
        tenant={tenant}
        name={PHONE_PROMPT}
        title={t('bot.prompt.phoneTitle')}
        subtitle={t('bot.prompt.phoneSubtitle')}
        prompt={byName(PHONE_PROMPT)}
      />
    </div>
  )
}

function PromptEditor({
  tenant,
  name,
  title,
  subtitle,
  prompt,
}: {
  tenant: string
  name: string
  title: string
  subtitle: string
  prompt?: Prompt
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const initial = prompt?.prompt ?? prompt?.content ?? ''
  const [text, setText] = useState(initial)
  useEffect(() => setText(initial), [initial])
  const dirty = text !== initial

  const save = useMutation({
    mutationFn: () => (prompt ? updatePromptText(tenant, name, text) : createPromptText(tenant, name, text)),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['prompts', tenant] }),
  })
  const saved = useSavedFlash(save.isSuccess ? save.submittedAt : 0)

  const modified = parseApiDate(prompt?.modified_at)

  return (
    <SectionCard title={title} subtitle={subtitle}>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (dirty) save.mutate()
        }}
        className="space-y-3"
      >
        <textarea
          className="input min-h-[22rem] resize-y font-sans text-sm leading-6"
          value={text}
          onChange={(e) => setText(e.target.value)}
          dir="auto"
          spellCheck={false}
          placeholder={t('bot.prompt.placeholder')}
        />
        <div className="flex flex-wrap items-center gap-3 text-xs text-ink-muted">
          <span>{t('bot.prompt.chars', { count: text.length })}</span>
          {modified && (
            <span>
              {t('bot.prompt.modified')}: {formatDateTime(modified)}
              {prompt?.modified_by ? ` · ${prompt.modified_by}` : ''}
            </span>
          )}
          <span className="ms-auto flex items-center gap-2">
            {dirty && (
              <button type="button" className="btn-ghost text-xs" onClick={() => setText(initial)}>
                <RotateCcw size={14} />
                {t('bot.prompt.discard')}
              </button>
            )}
            <SaveButton saving={save.isPending} saved={saved} disabled={!dirty} />
          </span>
        </div>
        <InlineError message={save.isError ? t('bot.prompt.saveFailed', { reason: errorDetail(save.error) }) : null} />
      </form>
    </SectionCard>
  )
}
