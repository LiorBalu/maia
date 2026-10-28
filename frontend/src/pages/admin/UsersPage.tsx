import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { createUser, deleteUser } from '@/api/admin'
import { listUsers, type ApiUser } from '@/api/auth'
import { listTenants } from '@/api/tenants'
import { useAuth } from '@/auth/AuthContext'
import { RoleBadge } from '@/components/Badges'
import { Field, InlineError, SaveButton } from '@/components/Form'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState, ErrorState, SkeletonCards } from '@/components/States'
import { errorDetail } from '@/lib/errors'
import { formatDateTime } from '@/lib/format'

export function UsersPage() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const { user: me } = useAuth()
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { data, isLoading, isError, refetch } = useQuery({ queryKey: ['users'], queryFn: listUsers })
  const users = [...(data ?? [])].sort((a, b) => a.email.localeCompare(b.email))

  const remove = useMutation({
    mutationFn: (email: string) => deleteUser(email),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['users'] }),
    onError: (e) => setError(t('users.deleteFailed', { reason: errorDetail(e) })),
  })

  return (
    <>
      <PageHeader
        title={t('users.title')}
        subtitle={t('users.subtitle')}
        actions={
          <div className="flex items-center gap-2">
            {data && <span className="badge bg-brand-100 text-brand-800">{t('users.count', { count: data.length })}</span>}
            <button type="button" className="btn-primary" onClick={() => setCreating(true)}>
              <Plus size={16} />
              {t('users.create.button')}
            </button>
          </div>
        }
      />
      {creating && <CreateUserForm onClose={() => setCreating(false)} />}
      <div className="mb-3">
        <InlineError message={error} />
      </div>
      {isLoading && <SkeletonCards count={3} />}
      {isError && <ErrorState onRetry={() => void refetch()} />}
      {data && users.length === 0 && <EmptyState text={t('users.empty')} />}
      {users.length > 0 && (
        <ul className="card divide-y divide-brand-50">
          {users.map((user) => (
            <UserRow
              key={user.email}
              user={user}
              isMe={user.email === me?.email}
              deleting={remove.isPending && remove.variables === user.email}
              onDelete={() => {
                if (window.confirm(t('users.deleteConfirm', { email: user.email }))) remove.mutate(user.email)
              }}
            />
          ))}
        </ul>
      )}
    </>
  )
}

function CreateUserForm({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const tenants = useQuery({ queryKey: ['tenants'], queryFn: listTenants })
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<'user' | 'staff'>('user')
  const [tenant, setTenant] = useState('')

  const passwordOk = /^(?=.*[A-Z])(?=.*[!@#$%^&*(),.?":{}|<>])[A-Za-z\d!@#$%^&*(),.?":{}|<>]{8,}$/.test(password)
  const valid = name.trim() && email.trim() && passwordOk && (role === 'staff' || tenant)

  const create = useMutation({
    mutationFn: () => createUser({ name: name.trim(), email: email.trim().toLowerCase(), password, role, tenants: role === 'staff' ? [] : [tenant] }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['users'] })
      void queryClient.invalidateQueries({ queryKey: ['tenants'] })
      onClose()
    },
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    if (valid) create.mutate()
  }

  const tenantNames = [...(tenants.data ?? [])].map((s) => s.name).sort((a, b) => a.localeCompare(b))

  return (
    <form onSubmit={submit} className="card mb-5 space-y-3 p-5">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-base font-semibold text-ink">{t('users.create.title')}</div>
          <div className="text-xs text-ink-soft">{t('users.create.subtitle')}</div>
        </div>
        <button type="button" className="btn-ghost p-1.5" onClick={onClose} aria-label={t('common.close')}>
          <X size={16} />
        </button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t('users.create.name')}>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} dir="auto" required autoFocus />
        </Field>
        <Field label={t('login.email')}>
          <input type="email" className="input text-left" dir="ltr" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="off" />
        </Field>
        <Field label={t('login.password')} help={t('users.create.passwordHelp')}>
          <input type="text" className="input text-left" dir="ltr" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="off" />
        </Field>
        <Field label={t('users.create.role')} help={t('users.create.roleHelp')}>
          <select className="input" value={role} onChange={(e) => setRole(e.target.value as 'user' | 'staff')}>
            <option value="user">{t('roles.user')}</option>
            <option value="staff">{t('roles.staff')}</option>
          </select>
        </Field>
        {role === 'user' && (
          <Field label={t('users.create.tenant')} className="sm:col-span-2">
            <select className="input" value={tenant} onChange={(e) => setTenant(e.target.value)} required>
              <option value="">{t('switcher.placeholder')}</option>
              {tenantNames.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </Field>
        )}
      </div>
      <div className="flex items-center justify-between gap-3">
        <InlineError message={create.isError ? t('users.create.failed', { reason: errorDetail(create.error) }) : null} />
        <div className="flex gap-2">
          <button type="button" className="btn-ghost" onClick={onClose}>
            {t('knowledge.qa.cancel')}
          </button>
          <SaveButton saving={create.isPending} disabled={!valid} label={t('users.create.submit')} />
        </div>
      </div>
    </form>
  )
}

function roleOf(user: ApiUser): 'staff' | 'user' {
  if (user.role === 'staff' || user.role === 'user') return user.role
  return user.is_staff ? 'staff' : 'user'
}

function UserRow({ user, isMe, deleting, onDelete }: { user: ApiUser; isMe: boolean; deleting: boolean; onDelete: () => void }) {
  const { t } = useTranslation()
  const role = roleOf(user)
  const initial = (user.name?.trim() || user.email).charAt(0).toUpperCase()
  const tenants = user.tenants_name ?? []
  const lastLogin = formatDateTime(user.last_login)
  return (
    <li className="flex flex-wrap items-center gap-4 px-5 py-4">
      <div className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-brand-100 font-semibold text-brand-800">
        {initial}
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate font-medium text-ink">{user.name && user.name !== 'name' ? user.name : user.email}</div>
        <div className="truncate text-xs text-ink-soft" dir="ltr">
          {user.email}
        </div>
      </div>
      <RoleBadge role={role} />
      <div className="flex max-w-xs flex-wrap gap-1">
        {role === 'staff' ? (
          <span className="text-xs text-ink-muted">{t('users.allTenants')}</span>
        ) : tenants.length > 0 ? (
          tenants.map((name) => (
            <span key={name} className="badge bg-brand-50 text-brand-800 ring-1 ring-brand-100">
              {name}
            </span>
          ))
        ) : (
          <span className="text-xs text-ink-muted">{t('users.noTenants')}</span>
        )}
      </div>
      <div className="text-xs text-ink-muted">
        {t('users.lastLogin')}: {lastLogin || t('users.never')}
      </div>
      {!isMe && (
        <button type="button" className="btn-ghost p-2 text-red-600 hover:bg-red-50" disabled={deleting} onClick={onDelete} aria-label={t('users.delete')} title={t('users.delete')}>
          <Trash2 size={15} />
        </button>
      )}
    </li>
  )
}
