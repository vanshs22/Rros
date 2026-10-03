import type { ReactNode } from 'react'
import type { ApiError } from '../api'

export function LoadingState({ label = 'Loading live workspace data…' }: { label?: string }) {
  return <div className="state" role="status"><span className="spinner" aria-hidden="true" /><span>{label}</span></div>
}

export function EmptyState({ title, detail = 'No records are available for this workspace yet.', action }: { title: string; detail?: string; action?: ReactNode }) {
  return <div className="state"><strong>{title}</strong><span>{detail}</span>{action}</div>
}

export function ErrorState({ error, retry }: { error: ApiError | Error | null; retry?: () => void }) {
  return <div className="state error" role="alert"><strong>Couldn’t load this view</strong><span>{error?.message || 'The API did not return data.'}</span>{retry && <button className="button secondary" onClick={retry}>Try again</button>}</div>
}

export function DataState<T>({ loading, error, data, retry, empty, children }: { loading: boolean; error: ApiError | null; data: T | null; retry: () => void; empty?: ReactNode; children: (value: T) => ReactNode }) {
  if (loading) return <LoadingState />
  if (error) return <ErrorState error={error} retry={retry} />
  if (data === null || (Array.isArray(data) && data.length === 0)) return empty ?? <EmptyState title="No records yet" />
  return <>{children(data)}</>
}
