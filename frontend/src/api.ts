// Типизированный клиент REST API строго по docs/DESIGN.md, раздел 12.
import type { Cmp } from './format'
import type { UploadCounters, UploadStatus } from './progress'

export class ApiError extends Error {
  status: number
  code: string
  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

type Query = Record<string, string | number | boolean | undefined | null>

function qs(q?: Query): string {
  if (!q) return ''
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null && v !== '') p.set(k, String(v))
  const s = p.toString()
  return s ? '?' + s : ''
}

async function request<T>(
  method: string,
  path: string,
  opts: { query?: Query; json?: unknown; body?: BodyInit; headers?: Record<string, string> } = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json', ...opts.headers }
  if (method !== 'GET') headers['X-Requested-With'] = 'ht'
  let body = opts.body
  if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(opts.json)
  }
  const res = await fetch('/api' + path + qs(opts.query), {
    method,
    headers,
    body,
    credentials: 'same-origin',
  })
  if (res.status === 204) return undefined as T
  const text = await res.text()
  let data: unknown = undefined
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = undefined
    }
  }
  if (!res.ok) {
    const e = (data as { error?: { code?: string; message?: string } } | undefined)?.error
    // 422 от FastAPI без нашей обёртки: {detail: ...}
    const detail = (data as { detail?: unknown } | undefined)?.detail
    const msg = e?.message ?? (typeof detail === 'string' ? detail : `Ошибка ${res.status}`)
    throw new ApiError(res.status, e?.code ?? 'http_' + res.status, msg)
  }
  return data as T
}

const get = <T>(path: string, query?: Query) => request<T>('GET', path, { query })
const post = <T>(path: string, json?: unknown) => request<T>('POST', path, { json })
const patch = <T>(path: string, json: unknown) => request<T>('PATCH', path, { json })
const put = <T>(path: string, json: unknown) => request<T>('PUT', path, { json })

// ---------- типы ----------
export interface User {
  id: number
  login: string
}
export interface Me extends User {
  timezone: string
  as_of: string | null
}

export interface Device {
  id: number
  external_id: string
  kind: string
  model: string | null
  name: string | null
  priority: number
  last_data_at: string | null
}

export type MetricStatus = 'no_data' | 'partial' | 'complete' | 'in_progress' | 'ok'
export type Coverage = 'none' | 'partial' | 'complete'

export interface GoalProgress {
  kind: GoalKind
  target: number
  value: number | null
  percent: number | null
  status: MetricStatus
  achieved: boolean
}

export interface DaySummary {
  date: string
  day_start: string
  day_end: string
  day_length_min: number
  coverage: Coverage
  wear_minutes: number
  stale?: boolean
  steps: { value: number | null; status: MetricStatus }
  active_minutes: number | null
  sleep: { minutes: number | null; status: MetricStatus; start: string | null; end: string | null }
  hr: { resting: number | null; peak: number | null; avg: number | null; status: MetricStatus }
}
export interface DayComparison {
  steps: Cmp
  sleep: Cmp
  resting_hr: Cmp
  peak_hr: Cmp
}
export interface DailyResponse extends DaySummary {
  comparison: DayComparison
  goals: GoalProgress[]
  processing: boolean
}
export interface RangeResponse {
  days: DaySummary[]
}
export interface WeeklyResponse {
  week: string
  from: string
  to: string
  status: 'complete' | 'partial' | 'no_data' | 'in_progress'
  valid_days_steps: number
  steps_avg: number | null
  steps_total: number | null
  sleep_avg: number | null
  sleep_nights: number
  resting_hr_avg: number | null
  peak_hr_max: number | null
  goal_days: number | null
  days: DaySummary[]
  comparison: DayComparison
}

export type GoalKind = 'steps_per_day' | 'sleep_minutes_per_day'
export interface Goal {
  kind: GoalKind
  target: number
  active: boolean
}
export interface Achievement {
  goal_kind: GoalKind
  local_date: string
  value: number
  target_at_time: number
  achieved_at: string
  revoked: boolean
}

export interface UploadItem extends UploadCounters {
  id: number
  filename: string | null
  size_bytes: number | null
  status: UploadStatus
  lines_done: number
  lines_total: number | null
  percent: number
  created_at: string
  finished_at: string | null
  min_ts?: string | null
  max_ts?: string | null
  error?: string | null
}
// В контракте «counters» — вложенный объект; допускаем и плоские поля (см. normalizeUpload).
export interface RawUpload extends Partial<UploadCounters> {
  id: number
  filename: string | null
  size_bytes: number | null
  status: UploadStatus
  lines_done: number
  lines_total: number | null
  percent: number
  counters?: Partial<UploadCounters>
  created_at: string
  finished_at: string | null
  min_ts?: string | null
  max_ts?: string | null
  error?: string | null
}
export function normalizeUpload(u: RawUpload): UploadItem {
  const c = u.counters ?? {}
  return {
    ...u,
    rows_inserted: c.rows_inserted ?? u.rows_inserted ?? 0,
    rows_updated: c.rows_updated ?? u.rows_updated ?? 0,
    rows_duplicate: c.rows_duplicate ?? u.rows_duplicate ?? 0,
    rows_rejected: c.rows_rejected ?? u.rows_rejected ?? 0,
  }
}
export interface UploadError {
  line_no: number
  code: string
  message: string
}

export interface GeneratorParams {
  from: string
  to: string
  timezone: string
  preset: string
  gaps: number
  seed?: number
}

export interface AppNotification {
  id: number
  kind: 'goal_achieved' | 'recommendation' | string
  payload: Record<string, unknown>
  created_at: string
  read_at: string | null
}

export interface Rule {
  id: string
  title: string
  description: string
  params: Record<string, unknown>
  enabled: boolean
  open_count: number
}
export interface Recommendation {
  id: number
  rule_id: string
  title: string
  body: string
  evidence: Record<string, unknown>
  status: 'open' | 'resolved'
  resolution_reason: 'auto_cleared' | 'rule_disabled' | 'expired' | null
  opened_at: string
  resolved_at: string | null
  feedback: 'helpful' | 'not_helpful' | null
}
export interface RecPage {
  items: Recommendation[]
  next_cursor: string | null
}

export interface TzPeriod {
  valid_from: string | null
  tz: string
  source: string
}
export interface QuietHours {
  enabled: boolean
  start: string
  end: string
}
export interface Settings {
  email: string | null
  email_enabled: boolean
  timezone: string
  timezone_periods: TzPeriod[]
  follow_device_tz: boolean
  quiet_hours: QuietHours
}

export interface OutboxItem {
  id: number
  subject: string
  status: 'pending' | 'sending' | 'sent' | 'failed' | 'dead' | 'cancelled'
  send_after: string | null
  defer_reason: 'quiet_hours' | 'hold' | null
  attempts: number
  sent_at: string | null
  recommendation_ids: number[]
  last_error?: string | null
  body_text?: string | null
}
export interface MailboxItem {
  id: number
  subject: string | null
  rcpt: string
  received_at: string
  body_text: string
}

// ---------- методы ----------
export const api = {
  register: (login: string, password: string) => post<User>('/auth/register', { login, password }),
  login: (login: string, password: string) => post<User>('/auth/login', { login, password }),
  logout: () => post<void>('/auth/logout'),
  me: () => get<Me>('/auth/me'),

  devices: () => get<Device[]>('/devices'),

  uploadFile: (file: File) =>
    request<{ upload_id: number; status: string }>('POST', '/uploads', {
      body: file,
      // имя файла только ASCII: заголовки HTTP не любят кириллицу
      headers: {
        'Content-Type': 'application/x-ndjson',
        'X-Filename': encodeURIComponent(file.name),
      },
    }),
  /** Не описано в DESIGN.md §12 — см. отчёт: POST /dev/generate, ставит ту же job process_upload. */
  generate: (p: GeneratorParams) =>
    request<{ upload_id: number; status: string }>('POST', '/dev/generate', { json: p }),
  uploads: async () => (await get<RawUpload[]>('/uploads')).map(normalizeUpload),
  uploadErrors: (id: number, limit = 100) => get<UploadError[]>(`/uploads/${id}/errors`, { limit }),
  cancelUpload: (id: number) => post<void>(`/uploads/${id}/cancel`),

  daily: (date: string) => get<DailyResponse>('/summary/daily', { date }),
  range: (from: string, to: string) => get<RangeResponse>('/summary/daily/range', { from, to }),
  weekly: (date: string) => get<WeeklyResponse>('/summary/weekly', { date }),

  goals: () => get<Goal[]>('/goals'),
  setGoal: (kind: GoalKind, target: number) => put<Goal>(`/goals/${kind}`, { target }),
  goalProgress: (date: string) => get<GoalProgress[]>('/goals/progress', { date }),
  achievements: (limit = 30) => get<Achievement[]>('/goals/achievements', { limit }),

  notifications: (unread = true, limit = 50) =>
    get<AppNotification[]>('/notifications', { unread: unread ? 1 : undefined, limit }),
  readNotification: (id: number) => post<void>(`/notifications/${id}/read`),
  readAllNotifications: () => post<void>('/notifications/read-all'),

  rules: () => get<Rule[]>('/rules'),
  setRule: (id: string, enabled: boolean) => patch<Rule>(`/rules/${id}`, { enabled }),
  recommendations: (status: 'open' | 'resolved' | 'all', cursor?: string | null, limit = 20) =>
    get<RecPage>('/recommendations', { status, limit, cursor }),
  feedback: (id: number, value: 'helpful' | 'not_helpful' | null) =>
    post<Recommendation>(`/recommendations/${id}/feedback`, { value }),

  settings: () => get<Settings>('/settings'),
  patchSettings: (p: {
    email?: string | null
    email_enabled?: boolean
    follow_device_tz?: boolean
    quiet_hours?: QuietHours
  }) => patch<Settings>('/settings', p),
  setTimezone: (p: { timezone: string; effective_from?: string; scope: 'from_now' | 'all_history' }) =>
    put<Settings>('/settings/timezone', p),

  outbox: (limit = 50) => get<OutboxItem[]>('/outbox', { limit }),
  mailbox: (limit = 50) => get<MailboxItem[]>('/dev/mailbox', { limit }),
}
