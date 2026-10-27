import { useState } from 'react'
import clsx from 'clsx'
import {
  BookOpen,
  Bot,
  Building2,
  CalendarDays,
  Inbox,
  LogOut,
  Menu,
  Settings,
  Users,
  X,
  type LucideIcon,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { NavLink, Outlet, useMatch, useNavigate } from 'react-router-dom'

import { useQuery } from '@tanstack/react-query'

import { getTenant } from '@/api/tenants'
import { useAuth } from '@/auth/AuthContext'
import { Logo } from '@/components/Logo'

import { TenantSwitcher } from './TenantSwitcher'

interface NavItem {
  to: string
  label: string
  icon: LucideIcon
}

export function AppShell() {
  const { t } = useTranslation()
  const { user, signOut } = useAuth()
  const navigate = useNavigate()
  const match = useMatch('/s/:tenantName/*')
  const tenantName = match?.params.tenantName
  const [drawerOpen, setDrawerOpen] = useState(false)
  const tenantQuery = useQuery({
    queryKey: ['tenant', tenantName],
    queryFn: () => getTenant(tenantName!),
    enabled: Boolean(tenantName),
    staleTime: 5 * 60_000,
  })
  const tenantTitle = tenantQuery.data?.description?.trim() || tenantName

  const tenantItems: NavItem[] = tenantName
    ? [
        { to: `/s/${tenantName}/inbox`, label: t('nav.inbox'), icon: Inbox },
        { to: `/s/${tenantName}/knowledge`, label: t('nav.knowledge'), icon: BookOpen },
        { to: `/s/${tenantName}/calendar`, label: t('nav.calendar'), icon: CalendarDays },
        { to: `/s/${tenantName}/bot`, label: t('nav.bot'), icon: Bot },
        { to: `/s/${tenantName}/settings`, label: t('nav.settings'), icon: Settings },
      ]
    : []

  const adminItems: NavItem[] = user?.isStaff
    ? [
        { to: '/admin/tenants', label: t('nav.tenants'), icon: Building2 },
        { to: '/admin/users', label: t('nav.users'), icon: Users },
      ]
    : []

  const closeDrawer = () => setDrawerOpen(false)

  const sidebar = (
    <nav className="flex h-full flex-col gap-6 overflow-y-auto p-4">
      <div className="flex items-center justify-between">
        <Logo />
        <button type="button" className="btn-ghost p-2 lg:hidden" onClick={closeDrawer} aria-label={t('common.close')}>
          <X size={18} />
        </button>
      </div>
      <TenantSwitcher onNavigate={closeDrawer} />
      {tenantItems.length > 0 && <NavSection title={t('nav.business')} items={tenantItems} onNavigate={closeDrawer} />}
      {adminItems.length > 0 && <NavSection title={t('nav.admin')} items={adminItems} onNavigate={closeDrawer} />}
      <div className="mt-auto rounded-xl bg-brand-50 px-3 py-2 text-xs text-ink-soft">
        <div className="truncate font-medium text-ink" dir="ltr">
          {user?.email}
        </div>
        <div>{t(`roles.${user?.role ?? 'user'}`)}</div>
      </div>
    </nav>
  )

  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-64 shrink-0 border-e border-brand-100 bg-white lg:block">{sidebar}</aside>

      {drawerOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div className="absolute inset-0 bg-ink/30" onClick={closeDrawer} />
          <aside className="absolute inset-y-0 start-0 w-72 bg-white shadow-pop">{sidebar}</aside>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-brand-100 bg-white/80 px-4 backdrop-blur lg:px-8">
          <button
            type="button"
            className="btn-ghost p-2 lg:hidden"
            onClick={() => setDrawerOpen(true)}
            aria-label={t('common.menu')}
          >
            <Menu size={20} />
          </button>
          <div className="min-w-0 flex-1 truncate text-sm text-ink-soft">
            {tenantName ? (
              <span>
                {t('nav.business')}: <span className="font-semibold text-ink" dir="auto">{tenantTitle}</span>
                {tenantTitle !== tenantName && (
                  <span className="ms-2 text-xs text-ink-muted" dir="ltr">
                    {tenantName}
                  </span>
                )}
              </span>
            ) : (
              <span className="font-semibold text-ink">{t('nav.admin')}</span>
            )}
          </div>
          <button
            type="button"
            className="btn-ghost"
            onClick={() => {
              signOut()
              navigate('/login', { replace: true })
            }}
          >
            <LogOut size={18} />
            <span className="hidden sm:inline">{t('common.logout')}</span>
          </button>
        </header>
        <main className="flex-1 px-4 py-6 lg:px-8">
          <Outlet />
        </main>
      </div>
    </div>
  )
}

function NavSection({ title, items, onNavigate }: { title: string; items: NavItem[]; onNavigate: () => void }) {
  return (
    <div>
      <div className="mb-2 px-3 text-xs font-semibold text-ink-muted">{title}</div>
      <ul className="space-y-1">
        {items.map((item) => (
          <li key={item.to}>
            <NavLink
              to={item.to}
              onClick={onNavigate}
              className={({ isActive }) => clsx('nav-item', isActive && 'nav-item-active')}
            >
              <item.icon size={18} className="shrink-0" />
              <span>{item.label}</span>
            </NavLink>
          </li>
        ))}
      </ul>
    </div>
  )
}
