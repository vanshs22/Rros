import { useEffect, useState } from 'react'
import { get } from '../api'
import { initials, label } from '../format'
import type { Me, SearchResult } from '../types'

export type PageKey = 'overview' | 'opportunities' | 'customers' | 'connectors' | 'agents' | 'automation' | 'setup'
const groups: Array<{ label: string; items: Array<{ key: PageKey; label: string; icon: string }> }> = [
  { label: 'Command', items: [{ key: 'overview', label: 'Command center', icon: '⌁' }, { key: 'opportunities', label: 'Opportunities', icon: '◇' }] },
  { label: 'Customers', items: [{ key: 'customers', label: 'Customer 360', icon: '◎' }] },
  { label: 'Operations', items: [{ key: 'connectors', label: 'Connectors', icon: '↯' }, { key: 'automation', label: 'Automation', icon: '⤢' }, { key: 'agents', label: 'Agents & traces', icon: '✳' }] },
  { label: 'Workspace', items: [{ key: 'setup', label: 'Admin & setup', icon: '⚙' }] },
]

export function Shell({ me, page, navigate, onLogout, children }: { me: Me; page: PageKey; navigate: (page: PageKey) => void; onLogout: () => void; children: React.ReactNode }) {
  const [mobileOpen, setMobileOpen] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  useEffect(() => { const handler = (event: KeyboardEvent) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); setSearchOpen(true) } if (event.key === 'Escape') setSearchOpen(false) }; window.addEventListener('keydown', handler); return () => window.removeEventListener('keydown', handler) }, [])
  return <div className="app-shell">
    <header className="topbar"><button className="button icon-button mobile-menu" aria-label={mobileOpen ? 'Close navigation' : 'Open navigation'} aria-expanded={mobileOpen} aria-controls="workspace-navigation" onClick={() => setMobileOpen((open) => !open)}>☰</button><div className="brand"><span className="brand-mark">R</span><span>Revenue<em>Recovery</em> OS</span></div><div className="topbar-actions"><button className="button secondary search-trigger" onClick={() => setSearchOpen(true)} aria-label="Search workspace">⌕ <span>Search</span><kbd>⌘ K</kbd></button><div className="workspace-chip">◈ {me.organization_slug || 'workspace'}</div><div className="user-chip"><span className="avatar">{initials(me.email)}</span><span>{me.email}</span></div><button className="button secondary" onClick={onLogout}>Log out</button></div></header>
    <div className="body-layout"><aside id="workspace-navigation" className={`sidebar ${mobileOpen ? 'open' : ''}`} aria-label="Workspace navigation">{groups.map((group) => <div className="sidebar-section" key={group.label}><div className="sidebar-label">{group.label}</div>{group.items.map((item) => <button key={item.key} className={`nav-item ${page === item.key ? 'active' : ''}`} onClick={() => { navigate(item.key); setMobileOpen(false) }}><span className="nav-icon" aria-hidden="true">{item.icon}</span>{item.label}</button>)}</div>)}<div className="sidebar-bottom"><strong>Safe by default</strong><span className="subtle">Actions stay approval-gated and provider-aware.</span><span className="role-badge">{label(me.role)}</span></div></aside><main className="main" id="main-content">{children}</main></div>
    {searchOpen && <SearchDialog close={() => setSearchOpen(false)} navigate={navigate} />}
  </div>
}

function SearchDialog({ close, navigate }: { close: () => void; navigate: (page: PageKey) => void }) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SearchResult[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { if (!query.trim()) { setResults([]); setError(''); return }; const timer = window.setTimeout(() => { setLoading(true); get<{ results?: SearchResult[] }>(`/api/search?q=${encodeURIComponent(query.trim())}`).then((payload) => setResults(Array.isArray(payload?.results) ? payload.results : [])).catch((caught) => setError(caught instanceof Error ? caught.message : 'Search failed')).finally(() => setLoading(false)) }, 250); return () => window.clearTimeout(timer) }, [query])
  const openResult = (result: SearchResult) => { const type = String(result.type || '').toLowerCase(); if (type.includes('customer')) navigate('customers'); else if (type.includes('opportun')) navigate('opportunities'); close() }
  return <div className="modal-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) close() }}><section className="search-modal" role="dialog" aria-modal="true" aria-labelledby="search-title"><div className="search-heading"><h2 id="search-title">Search workspace</h2><button className="button icon-button" onClick={close} aria-label="Close search">×</button></div><div className="search-input"><span aria-hidden="true">⌕</span><input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search customers, opportunities, and records…" aria-label="Search workspace" /></div><div className="search-results">{loading && <div className="state compact">Searching live records…</div>}{error && <div className="state error compact" role="alert">{error}</div>}{!loading && !error && query && results.length === 0 && <div className="state compact">No matching records returned.</div>}{results.map((result, index) => <button className="search-result" key={`${String(result.id || result.name || result.title)}-${index}`} onClick={() => openResult(result)}><strong>{String(result.title || result.name || result.label || result.id || 'Untitled result')}</strong><span>{label(result.type)}{result.description ? ` · ${String(result.description)}` : ''}</span></button>)}</div></section></div>
}
