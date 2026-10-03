import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, get } from './api'

export function useResource<T>(path: string, enabled = true) {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(enabled)
  const [error, setError] = useState<ApiError | null>(null)
  const generation = useRef(0)
  const load = useCallback(async () => {
    const requestGeneration = ++generation.current
    if (!enabled) { setLoading(false); return }
    setLoading(true)
    setError(null)
    try {
      const next = await get<T>(path)
      if (requestGeneration === generation.current) setData(next)
    } catch (caught) {
      if (requestGeneration === generation.current) setError(caught instanceof ApiError ? caught : new ApiError('Unable to load data', 0, caught))
    } finally {
      if (requestGeneration === generation.current) setLoading(false)
    }
  }, [enabled, path])
  useEffect(() => { void load(); return () => { generation.current += 1 } }, [load])
  return { data, loading, error, reload: load }
}

export function useAction() {
  const [busy, setBusy] = useState(false)
  const run = useCallback(async <T,>(action: () => Promise<T>) => { setBusy(true); try { return await action() } finally { setBusy(false) } }, [])
  return { busy, run }
}
