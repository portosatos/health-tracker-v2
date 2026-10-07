// SSE: отдельное соединение EventSource на каждую вкладку браузера (никакого общего состояния
// между вкладками). Last-Event-ID при реконнекте отправляет сам браузер.
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { mergeProgress, type ProgressState } from './progress'
import { useToasts } from './toasts'

const EVENT_TYPES = [
  'upload.progress',
  'upload.done',
  'upload.failed',
  'summary.updated',
  'goal.achieved',
  'recommendation.changed',
  'notification.created',
  'mail.status',
  'settings.tz_changed',
] as const

interface EventsCtx {
  progress: Record<number, ProgressState>
  connected: boolean
}
const Ctx = createContext<EventsCtx>({ progress: {}, connected: false })
export const useEvents = () => useContext(Ctx)

function normalizeProgress(d: Record<string, unknown>): ProgressState {
  const c = (d.counters as Record<string, number> | undefined) ?? {}
  const n = (k: string) => Number(d[k] ?? c[k] ?? 0)
  return {
    upload_id: Number(d.upload_id),
    status: d.status as ProgressState['status'],
    lines_done: n('lines_done'),
    lines_total: d.lines_total === null || d.lines_total === undefined ? null : Number(d.lines_total),
    percent: n('percent'),
    rows_inserted: n('rows_inserted'),
    rows_updated: n('rows_updated'),
    rows_duplicate: n('rows_duplicate'),
    rows_rejected: n('rows_rejected'),
  }
}

/** Сглаживает всплески: не чаще раза в `ms` перезапрашивает ключи (за время чанка может прийти много событий). */
function makeInvalidator(qc: QueryClient, ms = 400) {
  const pending = new Set<string>()
  let timer: ReturnType<typeof setTimeout> | null = null
  const flush = () => {
    timer = null
    for (const k of pending) void qc.invalidateQueries({ queryKey: [k] })
    pending.clear()
  }
  return {
    invalidate(...keys: string[]) {
      keys.forEach((k) => pending.add(k))
      if (!timer) timer = setTimeout(flush, ms)
    },
    stop() {
      if (timer) clearTimeout(timer)
    },
  }
}

export function EventsProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient()
  const toasts = useToasts()
  const toastRef = useRef(toasts)
  useEffect(() => {
    toastRef.current = toasts
  })
  const [progress, setProgress] = useState<Record<number, ProgressState>>({})
  const [connected, setConnected] = useState(false)
  const seenGoal = useRef(new Set<number>())

  useEffect(() => {
    const inv = makeInvalidator(qc)
    const es = new EventSource('/api/events')
    let wasDown = false
    es.onopen = () => {
      setConnected(true)
      if (wasDown) {
        // после обрыва: догоняем состояние обычными запросами (поверх Last-Event-ID)
        inv.invalidate('summary', 'uploads', 'goals', 'notifications', 'recommendations', 'rules', 'outbox')
      }
      wasDown = false
    }
    es.onerror = () => {
      setConnected(false)
      wasDown = true
    }
    const parse = (e: Event): Record<string, unknown> => {
      try {
        return JSON.parse((e as MessageEvent).data) as Record<string, unknown>
      } catch {
        return {}
      }
    }
    const handlers: Record<string, (d: Record<string, unknown>) => void> = {
      'upload.progress': (d) => {
        const p = normalizeProgress(d)
        setProgress((prev) => ({ ...prev, [p.upload_id]: mergeProgress(prev[p.upload_id], p) }))
      },
      'upload.done': (d) => {
        const p = normalizeProgress(d)
        setProgress((prev) => ({ ...prev, [p.upload_id]: mergeProgress(prev[p.upload_id], { ...p, percent: 100 }) }))
        inv.invalidate('uploads', 'summary', 'goals', 'recommendations', 'rules', 'outbox')
      },
      'upload.failed': (d) => {
        const p = normalizeProgress({ ...d, status: 'failed' })
        setProgress((prev) => ({ ...prev, [p.upload_id]: mergeProgress(prev[p.upload_id], p) }))
        inv.invalidate('uploads')
        toastRef.current.push('Загрузка завершилась ошибкой' + (d.error ? `: ${String(d.error)}` : ''), 'error')
      },
      'summary.updated': () => inv.invalidate('summary', 'goals'),
      'goal.achieved': (d) => {
        const id = Number(d.notification_id)
        if (!seenGoal.current.has(id)) {
          seenGoal.current.add(id)
          const what = d.goal_kind === 'steps_per_day' ? 'шаги' : 'сон'
          toastRef.current.push(`Цель достигнута: ${what} (${String(d.date)})`, 'success')
        }
        inv.invalidate('notifications', 'goals')
      },
      'recommendation.changed': () => inv.invalidate('recommendations', 'rules', 'notifications'),
      'notification.created': () => inv.invalidate('notifications'),
      'mail.status': () => inv.invalidate('outbox', 'mailbox'),
      'settings.tz_changed': () => {
        toastRef.current.push('Обнаружена смена часового пояса, сводки пересчитываются', 'info')
        inv.invalidate('settings', 'me', 'summary', 'goals')
      },
    }
    for (const t of EVENT_TYPES) es.addEventListener(t, (e) => handlers[t](parse(e)))
    return () => {
      inv.stop()
      es.close()
      setConnected(false)
    }
  }, [qc])

  return <Ctx.Provider value={{ progress, connected }}>{children}</Ctx.Provider>
}
