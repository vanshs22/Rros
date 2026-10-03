export const money = (value: unknown) => `$${Number(value || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}`
export const percent = (value: unknown) => `${Math.round(Number(value || 0) * 100)}%`
export const label = (value: unknown) => String(value ?? '—').replaceAll('_', ' ').replace(/\b\w/g, (m) => m.toUpperCase())
export const date = (value: unknown, withTime = false) => value ? new Date(String(value)).toLocaleString(undefined, withTime ? { dateStyle: 'medium', timeStyle: 'short' } : { dateStyle: 'medium' }) : '—'
export const initials = (email?: string) => String(email || '?').split('@')[0].slice(0, 2).toUpperCase()
export const tone = (value: unknown) => ['critical', 'high', 'error', 'failed', 'dead_letter', 'disconnected'].includes(String(value)) ? 'danger' : ['medium', 'pending', 'awaiting_approval', 'warning', 'configured'].includes(String(value)) ? 'warn' : ['connected', 'complete', 'completed', 'recovered', 'published', 'active', 'succeeded'].includes(String(value)) ? 'good' : ''
