// Чистые функции форматирования. Пропуск данных — всегда «нет данных», никогда «0».
export const NO_DATA = 'нет данных'

export type Status = 'no_data' | 'partial' | 'complete' | 'in_progress' | 'ok' | string

export interface Cmp {
  current: number | null
  previous: number | null
  delta_abs: number | null
  delta_pct: number | null
  comparable: boolean
  reason?: string | null
}

const nf = new Intl.NumberFormat('ru-RU')

export function formatNumber(v: number | null | undefined, unit = ''): string {
  if (v === null || v === undefined || Number.isNaN(v)) return NO_DATA
  return nf.format(Math.round(v)) + (unit ? ' ' + unit : '')
}

export function formatMinutes(min: number | null | undefined): string {
  if (min === null || min === undefined || Number.isNaN(min)) return NO_DATA
  const m = Math.round(min)
  const h = Math.floor(m / 60)
  const r = m % 60
  if (h === 0) return `${r} мин`
  return `${h} ч ${String(r).padStart(2, '0')} мин`
}

export function formatBpm(v: number | null | undefined): string {
  return formatNumber(v, 'уд/мин')
}

/** Метрика считается «отсутствующей», если значение null или статус no_data. */
export function isMissing(value: number | null | undefined, status?: Status): boolean {
  return value === null || value === undefined || status === 'no_data'
}

export function statusBadge(status?: Status): string | null {
  switch (status) {
    case 'partial':
      return 'неполные данные'
    case 'in_progress':
      return 'день не закончен'
    default:
      return null
  }
}

export interface DeltaView {
  arrow: '▲' | '▼' | '→' | ''
  text: string
  tone: 'up' | 'down' | 'flat' | 'none'
}

export const REASON_LABELS: Record<string, string> = {
  previous_no_data: 'нет данных за предыдущий период',
  current_no_data: 'нет данных за текущий период',
  insufficient_coverage: 'недостаточно дней с данными',
}

/** Строка сравнения с предыдущим периодом: стрелка и процент. */
export function formatDelta(cmp: Cmp | null | undefined): DeltaView {
  if (!cmp || !cmp.comparable || cmp.delta_abs === null) {
    const why = cmp?.reason ? (REASON_LABELS[cmp.reason] ?? cmp.reason) : 'сравнение недоступно'
    return { arrow: '', text: why, tone: 'none' }
  }
  const arrow = cmp.delta_abs > 0 ? '▲' : cmp.delta_abs < 0 ? '▼' : '→'
  const tone = cmp.delta_abs > 0 ? 'up' : cmp.delta_abs < 0 ? 'down' : 'flat'
  if (cmp.delta_pct === null) return { arrow, text: 'процент недоступен', tone }
  const sign = cmp.delta_pct > 0 ? '+' : cmp.delta_pct < 0 ? '−' : ''
  const pct = Math.abs(cmp.delta_pct).toLocaleString('ru-RU', { maximumFractionDigits: 1 })
  return { arrow, text: `${sign}${pct}%`, tone }
}

/** Процент выполнения цели; null, если значения нет. */
export function percentOf(value: number | null | undefined, target: number): number | null {
  if (value === null || value === undefined || !(target > 0)) return null
  return Math.round((value / target) * 100)
}

/** Расчёт delta_pct на клиенте (используется, если сервер его не прислал). */
export function calcDeltaPct(current: number | null, previous: number | null): number | null {
  if (current === null || previous === null || previous === 0) return null
  return ((current - previous) / previous) * 100
}

export function formatDateTime(iso: string | null | undefined, tz?: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString('ru-RU', {
    timeZone: tz,
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}
