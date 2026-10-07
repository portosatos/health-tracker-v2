import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type Settings } from '../api'
import { timeZones } from '../timezones'
import { formatDateTime } from '../format'

function Form({ s }: { s: Settings }) {
  const qc = useQueryClient()
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ['settings'] })
    void qc.invalidateQueries({ queryKey: ['me'] })
    void qc.invalidateQueries({ queryKey: ['summary'] })
  }

  // --- почта и тихие часы ---
  const [email, setEmail] = useState(s.email ?? '')
  const [emailEnabled, setEmailEnabled] = useState(s.email_enabled)
  const [follow, setFollow] = useState(s.follow_device_tz)
  const [qh, setQh] = useState(s.quiet_hours)
  useEffect(() => {
    setEmail(s.email ?? '')
    setEmailEnabled(s.email_enabled)
    setFollow(s.follow_device_tz)
    setQh(s.quiet_hours)
  }, [s])
  const save = useMutation({
    mutationFn: () =>
      api.patchSettings({
        email: email.trim() === '' ? null : email.trim(),
        email_enabled: emailEnabled,
        follow_device_tz: follow,
        quiet_hours: qh,
      }),
    onSuccess: refresh,
  })
  const wraps = qh.enabled && qh.start > qh.end

  // --- пояс ---
  const [tz, setTz] = useState(s.timezone)
  const [scope, setScope] = useState<'from_now' | 'all_history'>('from_now')
  const [from, setFrom] = useState('')
  const changeTz = useMutation({
    mutationFn: () =>
      api.setTimezone({
        timezone: tz,
        scope,
        effective_from: scope === 'from_now' && from ? new Date(from).toISOString() : undefined,
      }),
    onSuccess: refresh,
  })

  return (
    <>
      <form
        className="card form"
        onSubmit={(e) => {
          e.preventDefault()
          save.mutate()
        }}
      >
        <h3>Почта и уведомления</h3>
        <label>
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@example.com" />
        </label>
        <label className="check">
          <input type="checkbox" checked={emailEnabled} onChange={(e) => setEmailEnabled(e.target.checked)} />
          Присылать письма с рекомендациями
        </label>
        <label className="check">
          <input type="checkbox" checked={qh.enabled} onChange={(e) => setQh({ ...qh, enabled: e.target.checked })} />
          Тихие часы (письма не отправляются)
        </label>
        <div className="row gap">
          <label>
            С
            <input type="time" value={qh.start} disabled={!qh.enabled} onChange={(e) => setQh({ ...qh, start: e.target.value })} />
          </label>
          <label>
            До
            <input type="time" value={qh.end} disabled={!qh.enabled} onChange={(e) => setQh({ ...qh, end: e.target.value })} />
          </label>
        </div>
        {qh.enabled && (
          <small className="muted">
            {qh.start === qh.end
              ? 'Начало и конец совпадают - тихих часов нет.'
              : wraps
                ? `Интервал через полночь: с ${qh.start} до ${qh.end} следующих суток.`
                : `Интервал внутри суток: с ${qh.start} до ${qh.end}.`}{' '}
            Время - в вашем часовом поясе.
          </small>
        )}
        <label className="check">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
          Определять смену часового пояса по устройству
        </label>
        {save.isError && <p className="error">{save.error.message}</p>}
        {save.isSuccess && <p className="ok">Сохранено</p>}
        <button className="primary" disabled={save.isPending}>
          Сохранить
        </button>
      </form>

      <form
        className="card form"
        onSubmit={(e) => {
          e.preventDefault()
          changeTz.mutate()
        }}
      >
        <h3>Часовой пояс</h3>
        <p>
          Сейчас: <strong>{s.timezone}</strong>
        </p>
        <label>
          Новый пояс (IANA)
          <select value={tz} onChange={(e) => setTz(e.target.value)}>
            {timeZones(s.timezone).map((z) => (
              <option key={z}>{z}</option>
            ))}
          </select>
        </label>
        <fieldset>
          <legend>К чему применить</legend>
          <label className="check">
            <input type="radio" name="scope" checked={scope === 'from_now'} onChange={() => setScope('from_now')} />
            Только с этого момента (поездка, переезд): прошлые дни не меняются
          </label>
          <label className="check">
            <input type="radio" name="scope" checked={scope === 'all_history'} onChange={() => setScope('all_history')} />
            Ко всей истории (исправить ошибочную настройку): все сводки будут пересчитаны
          </label>
        </fieldset>
        {scope === 'from_now' && (
          <label>
            С какого момента (необязательно, по умолчанию - сейчас)
            <input type="datetime-local" value={from} onChange={(e) => setFrom(e.target.value)} />
          </label>
        )}
        {changeTz.isError && <p className="error">{changeTz.error.message}</p>}
        {changeTz.isSuccess && <p className="ok">Пояс изменён, пересчёт идёт в фоне</p>}
        <button className="primary" disabled={changeTz.isPending || (tz === s.timezone && scope === 'from_now' && !from)}>
          Применить
        </button>
        {s.timezone_periods.length > 1 && (
          <details>
            <summary>История поясов</summary>
            <ul>
              {s.timezone_periods.map((p, i) => (
                <li key={i}>
                  {p.valid_from ? formatDateTime(p.valid_from) : 'с начала'}: {p.tz} ({p.source})
                </li>
              ))}
            </ul>
          </details>
        )}
      </form>
    </>
  )
}

export default function SettingsPage() {
  const q = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  return (
    <div className="page">
      <h2>Настройки</h2>
      {q.isPending && <p className="muted">Загрузка…</p>}
      {q.isError && <p className="error">{q.error.message}</p>}
      {q.data && <Form s={q.data} />}
    </div>
  )
}
