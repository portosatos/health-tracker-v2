import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type Goal, type GoalKind, type GoalProgress, type Me } from '../api'
import { addDays, formatDayLong, todayIn } from '../dates'
import { formatMinutes, formatNumber, isMissing, percentOf, statusBadge } from '../format'

const LABELS: Record<GoalKind, string> = {
  steps_per_day: 'Шаги в день',
  sleep_minutes_per_day: 'Сон, часов в день',
}

function GoalEditor({ goal, kind }: { goal?: Goal; kind: GoalKind }) {
  const qc = useQueryClient()
  const isSleep = kind === 'sleep_minutes_per_day'
  const current = goal ? (isSleep ? goal.target / 60 : goal.target) : ''
  const [val, setVal] = useState<string>('')
  const save = useMutation({
    mutationFn: () => {
      const n = Number(val.replace(',', '.'))
      return api.setGoal(kind, isSleep ? Math.round(n * 60) : Math.round(n))
    },
    onSuccess: () => {
      setVal('')
      void qc.invalidateQueries({ queryKey: ['goals'] })
      void qc.invalidateQueries({ queryKey: ['summary'] })
    },
  })
  const n = Number(val.replace(',', '.'))
  const valid = val !== '' && n > 0 && Number.isFinite(n)
  return (
    <div className="card">
      <h3>{LABELS[kind]}</h3>
      <p>
        Сейчас: <strong>{goal ? (isSleep ? formatMinutes(goal.target) : formatNumber(goal.target)) : 'не задана'}</strong>
      </p>
      <div className="row gap">
        <input
          inputMode="decimal"
          placeholder={String(current || (isSleep ? 8 : 8000))}
          value={val}
          onChange={(e) => setVal(e.target.value)}
          aria-label={LABELS[kind]}
        />
        <button className="primary" disabled={!valid || save.isPending} onClick={() => save.mutate()}>
          Сохранить
        </button>
      </div>
      {save.isError && <p className="error">{save.error.message}</p>}
    </div>
  )
}

function ProgressCard({ p }: { p: GoalProgress }) {
  const isSleep = p.kind === 'sleep_minutes_per_day'
  const missing = isMissing(p.value, p.status)
  const pct = missing ? null : (p.percent ?? percentOf(p.value, p.target))
  const badge = statusBadge(p.status)
  return (
    <div className="card">
      <div className="row between">
        <h3>{LABELS[p.kind].replace(', часов в день', '')}</h3>
        {p.achieved && <span className="badge badge-ok">достигнута</span>}
      </div>
      <div className={missing ? 'metric-value nodata' : 'metric-value'}>
        {missing ? 'нет данных' : isSleep ? formatMinutes(p.value) : formatNumber(p.value)}
        <small className="muted"> / {isSleep ? formatMinutes(p.target) : formatNumber(p.target)}</small>
      </div>
      <div className={missing ? 'bar bar-empty' : 'bar'}>
        <div className="bar-fill" style={{ width: `${Math.min(100, pct ?? 0)}%` }} />
      </div>
      <small>{pct === null ? 'прогресс: нет данных' : `${pct}%`}</small> {badge && <span className="badge badge-warn">{badge}</span>}
    </div>
  )
}

export default function Goals({ me }: { me: Me }) {
  const [date, setDate] = useState(me.as_of ?? todayIn(me.timezone))
  const goals = useQuery({ queryKey: ['goals', 'list'], queryFn: api.goals })
  const progress = useQuery({ queryKey: ['goals', 'progress', date], queryFn: () => api.goalProgress(date) })
  const ach = useQuery({ queryKey: ['goals', 'achievements'], queryFn: () => api.achievements(30) })
  const find = (k: GoalKind) => goals.data?.find((g) => g.kind === k)
  return (
    <div className="page">
      <h2>Цели</h2>
      <div className="grid">
        <GoalEditor kind="steps_per_day" goal={find('steps_per_day')} />
        <GoalEditor kind="sleep_minutes_per_day" goal={find('sleep_minutes_per_day')} />
      </div>

      <div className="row gap wrap datenav">
        <button onClick={() => setDate(addDays(date, -1))}>←</button>
        <input type="date" value={date} onChange={(e) => e.target.value && setDate(e.target.value)} />
        <button onClick={() => setDate(addDays(date, 1))}>→</button>
        <strong>{formatDayLong(date)}</strong>
      </div>
      {progress.isError && <p className="error">{progress.error.message}</p>}
      <div className="grid">
        {progress.data?.map((p) => <ProgressCard key={p.kind} p={p} />)}
        {progress.data?.length === 0 && <p className="muted">Задайте цель, чтобы видеть прогресс.</p>}
      </div>

      <h3>Достижения</h3>
      {ach.data?.length === 0 && <p className="muted">Пока нет достижений</p>}
      <ul className="plain stack">
        {ach.data?.map((a) => (
          <li key={`${a.goal_kind}-${a.local_date}`} className={a.revoked ? 'card revoked' : 'card'}>
            {LABELS[a.goal_kind].replace(', часов в день', '')} - {a.local_date}:{' '}
            {a.goal_kind === 'sleep_minutes_per_day' ? formatMinutes(a.value) : formatNumber(a.value)} из{' '}
            {a.goal_kind === 'sleep_minutes_per_day' ? formatMinutes(a.target_at_time) : formatNumber(a.target_at_time)}
            {a.revoked && <span className="badge badge-warn"> отозвано пересчётом</span>}
          </li>
        ))}
      </ul>
    </div>
  )
}
