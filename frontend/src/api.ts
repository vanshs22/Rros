export class ApiError extends Error {
  status: number
  details: unknown

  constructor(message: string, status: number, details?: unknown) { super(message); this.name = 'ApiError'; this.status = status; this.details = details }
}

const baseUrl = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
function csrfToken(): string { const item = document.cookie.split('; ').find((entry) => entry.startsWith('rros_csrf=')); return item ? decodeURIComponent(item.slice('rros_csrf='.length)) : '' }
function errorMessage(payload: unknown, status: number): string {
  if (typeof payload === 'object' && payload !== null && 'detail' in payload) { const detail = (payload as { detail?: unknown }).detail; if (typeof detail === 'string') return detail; if (typeof detail === 'object' && detail !== null && 'message' in detail) return String((detail as { message: unknown }).message); if (detail !== undefined) return JSON.stringify(detail) }
  if (typeof payload === 'object' && payload !== null && 'error' in payload) { const error = (payload as { error?: unknown }).error; if (typeof error === 'object' && error !== null && 'message' in error) return String((error as { message: unknown }).message); if (typeof error === 'string') return error }
  return `Request failed (${status})`
}
export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase(); const headers = new Headers(init.headers)
  if (init.body && typeof init.body !== 'string') { headers.set('Content-Type', 'application/json'); init = { ...init, body: JSON.stringify(init.body) } }
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) { const token = csrfToken(); if (token) headers.set('X-CSRF-Token', token) }
  const response = await fetch(`${baseUrl}${path}`, { ...init, method, headers, credentials: 'include' }); const contentType = response.headers.get('content-type') ?? ''; const payload: unknown = contentType.includes('json') ? await response.json().catch(() => null) : await response.text().catch(() => '')
  if (!response.ok) { if (response.status === 401) window.dispatchEvent(new CustomEvent('rros:unauthorized')); throw new ApiError(errorMessage(payload, response.status), response.status, payload) }
  return payload as T
}
export const get = <T>(path: string) => request<T>(path)
export const post = <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body), headers: body === undefined ? undefined : { 'Content-Type': 'application/json' } })
export const patch = <T>(path: string, body: unknown) => request<T>(path, { method: 'PATCH', body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } })
