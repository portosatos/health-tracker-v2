import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, type DaySummary, type Me } from '../api'
import { addDays, formatDay, formatDayLong, todayIn, weekStart } from '../dates'
import {
  NO_DATA,
  formatBpm,
  formatDelta,
  formatMinutes,
  formatNumber,
  isMissing,
  statusBadge,
  type Cmp,
  type Status,
} from '../format'
import { useEvents } from '../sse'
import { isTerminal } from '../progress'

function Metric(props: {
  title: string
  value: string
  missing: boolean
  status?: Status
  cmp?: Cmp
  hint?: string
}) {
  const badge = statusBadge(props.status)
  const d = props.cmp ? formatDelta(props.cmp) : null
  return (
    <div className="card metric">
      <div className="metric-title">{props.title}</div>
      <div className={props.missing ? 'metric-value nodata' : 'metric-value'}>{props.value}</div>
      {badge && !props.missing && <span className="badge badge-warn">{badge}</span>}
      {props.hint && <small className="muted">{props.hint}</small>}
      {d && (
        <div className={`delta delta-${d.tone}`} title="Сравнение с предыдущим периодом">
          {d.arrow && <span>{d.arrow}</span>} {d.text}
        </div>
      )}
    </div>
  )
}

interface ChartRow {
  label: string
  steps: number | null
  sleepH: number | null
  resting: number | null
  peak: number | null
}
function toRows(days: DaySummary[]): ChartRow[] {
  return days.map((d) => ({
    label: formatDay(d.date),
    // null остаётся null: Recharts не рисует столбец и разрывает линию
    steps: isMissing(d.steps.value, d.steps.status) ? null : d.steps.value,
    sleepH: isMissing(d.sleep.minutes, d.sleep.status) ? null : Math.round(((d.sleep.minutes ?? 0) / 60) * 10) / 10,
    resting: d.hr.resting,
    peak: isMissing(d.hr.peak, d.hr.status) ? null : d.hr.peak,
  }))
}

function Charts({ days }: { days: DaySummary[] }) {
  const rows = toRows(days)
  const empty = rows.every((r) => r.steps === null && r.sleepH === null && r.resting === null && r.peak === null)
  if (empty) return <div className="card muted">Для выбранного периода {NO_DATA}</div>
  const fmtTip = (v: unknown) => (v === null || v === undefined ? NO_DATA : String(v))
  return (
    <div className="charts">
      <div className="card">
        <h3>Шаги</h3>
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={rows}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="label" fontSize={11} />
            <YAxis fontSize={11} />
            <Tooltip formatter={fmtTip} />
            <Bar dataKey="steps" name="Шаги" fill="#3b82f6" />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="card">
        <h3>Сон, часы</h3>
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={rows}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="label" fontSize={11} />
            <YAxis fontSize={11} />
            <Tooltip formatter={fmtTip} />
            <Bar dataKey="sleepH" name="Сон, ч" fill="#8b5cf6" />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="card">
        <h3>Пульс</h3>
        <ResponsiveContainer width="100%" height={200}>
          <LineChart data={rows}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="label" fontSize={11} />
            <YAxis fontSize={11} domain={['auto', 'auto']} />
            <Tooltip formatter={fmtTip} />
            <Legend />
            <Line dataKey="resting" name="Покоя" stroke="#10b981" connectNulls={false} dot />
            <Line dataKey="peak" name="Пиковый" stroke="#ef4444" connectNulls={false} dot />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

export default function Dashboard({ me }: { me: Me }) {
  const [sp, setSp] = useSearchParams()
  const mode = sp.get('mode') === 'week' ? 'week' : 'day'
  const date = sp.get('date') ?? me.as_of ?? todayIn(me.timezone)
  const set = (patch: Record<string, string>) => {
    const next = new URLSearchParams(sp)
    for (const [k, v] of Object.entries(patch)) next.set(k, v)
    setSp(next, { replace: true })
  }
  const step = mode === 'week' ? 7 : 1
  const { progress } = useEvents()
  const running = Object.values(progress).filter((p) => !isTerminal(p.status))

  const daily = useQuery({ queryKey: ['summary', 'daily', date], queryFn: () => api.daily(date), enabled: mode === 'day' })
  const weekly = useQuery({
    queryKey: ['summary', 'weekly', weekStart(date)],
    queryFn: () => api.weekly(date),
    enabled: mode === 'week',
  })
  const chartFrom = mode === 'week' ? addDays(weekStart(date), -21) : addDays(date, -13)
  const chartTo = mode === 'week' ? addDays(weekStart(date), 6) : date
  const range = useQuery({
    queryKey: ['summary', 'range', chartFrom, chartTo],
    queryFn: () => api.range(chartFrom, chartTo),
  })

  const d = daily.data
  const w = weekly.data
  const err = (mode === 'day' ? daily.error : weekly.error) as Error | null
  const title = mode === 'day' ? formatDayLong(date) : w ? `Неделя ${w.week}: ${formatDay(w.from)} – ${formatDay(w.to)}` : 'Неделя'

  return (
    <div className="page">
      <div className="row between wrap">
        <h2>Сводка</h2>
        <div className="seg">
          <button className={mode === 'day' ? 'on' : ''} onClick={() => set({ mode: 'day' })}>
            День
          </button>
          <button className={mode === 'week' ? 'on' : ''} onClick={() => set({ mode: 'week' })}>
            Неделя
          </button>
        </div>
      </div>

      <div className="row gap wrap datenav">
        <button onClick={() => set({ date: addDays(date, -step) })} aria-label="Назад">
          ←
        </button>
        <input type="date" value={date} onChange={(e) => e.target.value && set({ date: e.target.value })} />
        <button onClick={() => set({ date: addDays(date, step) })} aria-label="Вперёд">
          →
        </button>
        {me.as_of && <button className="link" onClick={() => set({ date: me.as_of! })}>К последним данным</button>}
        <strong className="grow">{title}</strong>
      </div>

      {running.length > 0 && (
        <div className="card info">
          Идёт обработка файла ({running[0].percent}%), цифры обновляются по мере загрузки.
          <div className="bar">
            <div className="bar-fill" style={{ width: `${running[0].percent}%` }} />
          </div>
        </div>
      )}

      {err && <p className="error">{err.message}</p>}

      {mode === 'day' && d && (
        <>
          <div className="row gap wrap">
            {d.coverage !== 'complete' && (
              <span className={d.coverage === 'none' ? 'badge' : 'badge badge-warn'}>
                {d.coverage === 'none' ? 'нет данных за день' : 'частичные данные за день'}
              </span>
            )}
            {d.stale && <span className="badge badge-warn">пересчитывается</span>}
            {d.processing && <span className="badge badge-info">обработка…</span>}
            {d.day_length_min !== 1440 && (
              <span className="badge">день {formatMinutes(d.day_length_min)} (смена пояса)</span>
            )}
          </div>
          <div className="grid">
            <Metric
              title="Шаги"
              value={formatNumber(d.steps.value)}
              missing={isMissing(d.steps.value, d.steps.status)}
              status={d.steps.status}
              cmp={d.comparison.steps}
              hint={d.active_minutes !== null ? `активных минут: ${d.active_minutes}` : undefined}
            />
            <Metric
              title="Сон"
              value={formatMinutes(d.sleep.minutes)}
              missing={isMissing(d.sleep.minutes, d.sleep.status)}
              status={d.sleep.status}
              cmp={d.comparison.sleep}
            />
            <Metric
              title="Пульс покоя"
              value={formatBpm(d.hr.resting)}
              missing={d.hr.resting === null}
              status={d.hr.status}
              cmp={d.comparison.resting_hr}
            />
            <Metric
              title="Пиковый пульс"
              value={formatBpm(d.hr.peak)}
              missing={isMissing(d.hr.peak, d.hr.status)}
              status={d.hr.status}
              cmp={d.comparison.peak_hr}
            />
          </div>
        </>
      )}

      {mode === 'week' && w && (
        <>
          <div className="row gap wrap">
            {w.status !== 'complete' && (
              <span className={w.status === 'no_data' ? 'badge' : 'badge badge-warn'}>
                {w.status === 'no_data'
                  ? 'нет данных за неделю'
                  : w.status === 'in_progress'
                    ? 'неделя не закончена'
                    : 'частичные данные за неделю'}
              </span>
            )}
            <span className="badge">дней с полными данными по шагам: {w.valid_days_steps}</span>
          </div>
          <div className="grid">
            <Metric
              title="Шаги, среднее в день"
              value={formatNumber(w.steps_avg)}
              missing={w.steps_avg === null}
              status={w.status === 'partial' ? 'partial' : undefined}
              cmp={w.comparison.steps}
              hint={w.steps_total !== null ? `всего: ${formatNumber(w.steps_total)}` : undefined}
            />
            <Metric
              title="Сон, среднее за ночь"
              value={formatMinutes(w.sleep_avg)}
              missing={w.sleep_avg === null}
              cmp={w.comparison.sleep}
              hint={`ночей с данными: ${w.sleep_nights}`}
            />
            <Metric
              title="Пульс покоя, среднее"
              value={formatBpm(w.resting_hr_avg)}
              missing={w.resting_hr_avg === null}
              cmp={w.comparison.resting_hr}
            />
            <Metric
              title="Пиковый пульс, максимум"
              value={formatBpm(w.peak_hr_max)}
              missing={w.peak_hr_max === null}
              cmp={w.comparison.peak_hr}
            />
          </div>
        </>
      )}

      {(daily.isPending && mode === 'day') || (weekly.isPending && mode === 'week') ? (
        <p className="muted">Загрузка…</p>
      ) : null}

      <h3>{mode === 'week' ? 'Последние недели по дням' : 'Последние 14 дней'}</h3>
      {range.data ? <Charts days={range.data.days} /> : <p className="muted">Загрузка графиков…</p>}
    </div>
  )
}
