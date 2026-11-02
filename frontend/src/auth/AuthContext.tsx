import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'

import { login as apiLogin } from '@/api/auth'

import { clearSession, getToken, parseUser, setToken, type SessionUser } from './session'

export type SignInError = 'failed' | 'server'

interface AuthContextValue {
  user: SessionUser | null
  signIn: (email: string, password: string) => Promise<void>
  signOut: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<SessionUser | null>(() => {
    const token = getToken()
    return token ? parseUser(token) : null
  })

  const signIn = useCallback(async (email: string, password: string) => {
    let response
    try {
      response = await apiLogin(email, password)
    } catch (error: unknown) {
      const status = (error as { response?: { status?: number } })?.response?.status
      throw new Error(status ? 'failed' : 'server')
    }
    if (!response.token) throw new Error('failed')
    const parsed = parseUser(response.token)
    if (!parsed) throw new Error('failed')
    setToken(response.token)
    setUser(parsed)
  }, [])

  const signOut = useCallback(() => {
    clearSession()
    setUser(null)
  }, [])

  const value = useMemo(() => ({ user, signIn, signOut }), [user, signIn, signOut])
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}
