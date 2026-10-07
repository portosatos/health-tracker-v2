// Склейка прогресса загрузки из REST-списка и SSE-событий `upload.progress`.
export type UploadStatus =
  | 'queued'
  | 'processing'
  | 'done'
  | 'done_with_errors'
  | 'failed'
  | 'cancelled'

export interface UploadCounters {
  rows_inserted: number
  rows_updated: number
  rows_duplicate: number
  rows_rejected: number
}

export interface ProgressState extends UploadCounters {
  upload_id: number
  status: UploadStatus
  lines_done: number
  lines_total: number | null
  percent: number
}

export const TERMINAL: UploadStatus[] = ['done', 'done_with_errors', 'failed', 'cancelled']
export const isTerminal = (s: UploadStatus) => TERMINAL.includes(s)

/**
 * События могут прийти не по порядку (REST-ответ позже SSE и наоборот): прогресс монотонен,
 * терминальный статус не откатывается.
 */
export function mergeProgress(prev: ProgressState | undefined, next: ProgressState): ProgressState {
  if (!prev) return next
  if (isTerminal(prev.status) && !isTerminal(next.status)) return prev
  if (isTerminal(next.status)) return { ...next, lines_done: Math.max(prev.lines_done, next.lines_done) }
  if (next.lines_done < prev.lines_done) return prev
  return { ...next, percent: Math.max(prev.percent, next.percent) }
}

export function computePercent(done: number, total: number | null): number {
  if (!total || total <= 0) return 0
  return Math.min(100, Math.floor((done / total) * 100))
}

export const STATUS_LABELS: Record<UploadStatus, string> = {
  queued: 'в очереди',
  processing: 'обрабатывается',
  done: 'готово',
  done_with_errors: 'готово, есть ошибки строк',
  failed: 'ошибка',
  cancelled: 'отменено',
}
