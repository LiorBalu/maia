import { Navigate, Route, Routes } from 'react-router-dom'

import { HomeRedirect, RequireAuth, RequireTenantAccess, RequireStaff } from './auth/guards'
import { AppShell } from './layout/AppShell'
import { LoginPage } from './pages/LoginPage'
import { TenantsPage } from './pages/admin/TenantsPage'
import { UsersPage } from './pages/admin/UsersPage'
import { BotPage } from './pages/tenant/BotPage'
import { CalendarPage } from './pages/tenant/CalendarPage'
import { InboxPage } from './pages/tenant/InboxPage'
import { KnowledgePage } from './pages/tenant/KnowledgePage'
import { SettingsPage } from './pages/tenant/SettingsPage'

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      <Route element={<RequireAuth />}>
        <Route element={<AppShell />}>
          <Route path="/" element={<HomeRedirect />} />

          <Route element={<RequireStaff />}>
            <Route path="/admin" element={<Navigate to="/admin/tenants" replace />} />
            <Route path="/admin/tenants" element={<TenantsPage />} />
            <Route path="/admin/users" element={<UsersPage />} />
          </Route>

          <Route path="/s/:tenantName" element={<RequireTenantAccess />}>
            <Route index element={<Navigate to="inbox" replace />} />
            <Route path="inbox" element={<InboxPage />} />
            <Route path="knowledge" element={<KnowledgePage />} />
            <Route path="calendar" element={<CalendarPage />} />
            <Route path="bot" element={<BotPage />} />
            <Route path="settings" element={<SettingsPage />} />
          </Route>
        </Route>
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
