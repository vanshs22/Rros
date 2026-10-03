import { useEffect, useState } from 'react'
import { get, post } from './api'
import { Shell, type PageKey } from './components/Shell'
import { LoadingState } from './components/States'
import { LoginPage } from './pages/LoginPage'
import { CommandCenterPage } from './pages/CommandCenterPage'
import { OpportunitiesPage } from './pages/OpportunitiesPage'
import { CustomersPage } from './pages/CustomersPage'
import { ConnectorsPage } from './pages/ConnectorsPage'
import { AgentsPage } from './pages/AgentsPage'
import { AutomationPage } from './pages/AutomationPage'
import { SetupPage } from './pages/SetupPage'
import type { Me } from './types'

type AuthStatus = 'loading' | 'anonymous' | 'authenticated'
const validPages = new Set<PageKey>(['overview', 'opportunities', 'customers', 'connectors', 'agents', 'automation', 'setup'])
function pageFromLocation(): PageKey { const value = window.location.hash.replace(/^#\/?/, '') as PageKey; return validPages.has(value) ? value : 'overview' }
export default function App() { const [me, setMe] = useState<Me | null>(null); const [auth, setAuth] = useState<AuthStatus>('loading'); const [page, setPage] = useState<PageKey>(pageFromLocation)
  useEffect(() => { const unauthorized = () => { setMe(null); setAuth('anonymous') }; const locationChanged = () => setPage(pageFromLocation()); window.addEventListener('rros:unauthorized', unauthorized); window.addEventListener('hashchange', locationChanged); get<Me>('/api/me').then((value) => { setMe(value); setAuth('authenticated') }).catch(() => setAuth('anonymous')); return () => { window.removeEventListener('rros:unauthorized', unauthorized); window.removeEventListener('hashchange', locationChanged) } }, [])
  const navigate = (next: PageKey) => { window.location.hash = next; setPage(next) }
  const submitAuth = async (mode: 'signin' | 'signup', values: Record<string, string>) => { if (mode === 'signin') await post('/api/auth/login', { email: values.email, password: values.password, organization_slug: values.organization_slug || null }); else await post('/api/auth/register', { organization_name: values.organization_name, organization_slug: values.organization_slug, email: values.email, password: values.password }); setMe(await get<Me>('/api/me')); setAuth('authenticated'); navigate('overview') }
  const logout = async () => { try { await post('/api/auth/logout') } finally { setMe(null); setAuth('anonymous'); window.location.hash = '' } }
  if (auth === 'loading') return <LoadingState label="Checking your RROS session…" />
  if (!me || auth === 'anonymous') return <LoginPage submit={submitAuth} />
  const canManage = ['owner', 'admin', 'manager'].includes(me.role); const isAdmin = ['owner', 'admin'].includes(me.role)
  return <Shell me={me} page={page} navigate={navigate} onLogout={() => void logout()}>{page === 'overview' && <CommandCenterPage go={navigate} canManage={canManage} />}{page === 'opportunities' && <OpportunitiesPage canManage={canManage} />}{page === 'customers' && <CustomersPage canManage={canManage} />}{page === 'connectors' && <ConnectorsPage canManage={isAdmin} />}{page === 'agents' && <AgentsPage canManage={canManage} />}{page === 'automation' && <AutomationPage canManage={canManage} isAdmin={isAdmin} />}{page === 'setup' && <SetupPage isAdmin={isAdmin} />}</Shell>
}
