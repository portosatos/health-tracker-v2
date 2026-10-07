import { useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type GeneratorParams, type Me, type UploadItem } from '../api'
import { useEvents } from '../sse'
import { useToasts } from '../toasts'
import { STATUS_LABELS, isTerminal, mergeProgress, type ProgressState } from '../progress'
import { addDays, todayIn } from '../dates'
import { formatDateTime, formatNumber } from '../format'
import { timeZones } from '../timezones'

const PRESETS: [string, string][] = [
  ['normal', 'Обычный человек (часы + телефон)'],
  ['active', 'Активный образ жизни'],
  ['sedentary', 'Малоподвижный'],
  ['poor_sleep', 'Плохой сон в конце периода'],
  ['travel', 'Поездка со сменой пояса'],
]

function toProgress(u: UploadItem): ProgressState {
  return {
    upload_id: u.id,
    status: u.status,
    lines_done: u.lines_done,
    lines_total: u.lines_total,
    percent: u.percent,
    rows_inserted: u.rows_inserted,
    rows_updated: u.rows_updated,
    rows_duplicate: u.rows_duplicate,
    rows_rejected: u.rows_rejected,
  }
}

function ErrorsList({ id }: { id: number }) {
  const q = useQuery({ queryKey: ['uploads', id, 'errors'], queryFn: () => api.uploadErrors(id, 100) })
  if (q.isPending) return <p className="muted">Загрузка…</p>
  if (!q.data?.length) return <p className="muted">Ошибок строк нет</p>
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Строка</th>
          <th>Код</th>
          <th>Сообщение</th>
        </tr>
      </thead>
      <tbody>
        {q.data.map((e) => (
          <tr key={e.line_no}>
            <td>{e.line_no}</td>
            <td>
              <code>{e.code}</code>
            </td>
            <td>{e.message}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function UploadRow({ u, live, onCancel }: { u: UploadItem; live?: ProgressState; onCancel: () => void }) {
  const [open, setOpen] = useState(false)
  const p = mergeProgress(toProgress(u), live ?? toProgress(u))
  const active = !isTerminal(p.status)
  return (
    <li className="card upload">
      <div className="row between wrap">
        <div>
          <strong>{u.filename ?? `Загрузка #${u.id}`}</strong>{' '}
          <span className={`badge badge-${p.status}`}>{STATUS_LABELS[p.status]}</span>
        </div>
        <small className="muted">{formatDateTime(u.created_at)}</small>
      </div>
      <div className="bar" aria-label="Прогресс">
        <div className="bar-fill" style={{ width: `${p.status === 'done' || p.status === 'done_with_errors' ? 100 : p.percent}%` }} />
      </div>
      <div className="row gap wrap small">
        <span>{p.percent}%</span>
        <span>
          строк: {formatNumber(p.lines_done)}
          {p.lines_total !== null ? ` из ${formatNumber(p.lines_total)}` : ''}
        </span>
        <span>новых: {formatNumber(p.rows_inserted)}</span>
        <span>обновлено: {formatNumber(p.rows_updated)}</span>
        <span>дубликатов: {formatNumber(p.rows_duplicate)}</span>
        <span className={p.rows_rejected ? 'error' : ''}>отклонено: {formatNumber(p.rows_rejected)}</span>
      </div>
      {u.error && <p className="error">{u.error}</p>}
      <div className="row gap">
        {active && (
          <button onClick={onCancel}>Отменить</button>
        )}
        {p.rows_rejected > 0 && (
          <button className="link" onClick={() => setOpen((o) => !o)}>
            {open ? 'Скрыть ошибки строк' : 'Ошибки строк'}
          </button>
        )}
      </div>
      {open && <ErrorsList id={u.id} />}
    </li>
  )
}

export default function Uploads({ me }: { me: Me }) {
  const qc = useQueryClient()
  const toasts = useToasts()
  const { progress } = useEvents()
  const fileRef = useRef<HTMLInputElement>(null)
  const list = useQuery({ queryKey: ['uploads'], queryFn: api.uploads })

  const afterStart = () => {
    toasts.push('Файл принят в обработку', 'info')
    void qc.invalidateQueries({ queryKey: ['uploads'] })
  }
  const upload = useMutation({
    mutationFn: (f: File) => api.uploadFile(f),
    onSuccess: () => {
      afterStart()
      if (fileRef.current) fileRef.current.value = ''
    },
  })
  const cancel = useMutation({
    mutationFn: api.cancelUpload,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['uploads'] }),
  })

  const today = todayIn(me.timezone)
  const [gen, setGen] = useState<GeneratorParams>({
    from: addDays(today, -30),
    to: addDays(today, -1),
    timezone: me.timezone,
    preset: 'normal',
    gaps: 0.1,
  })
  const generate = useMutation({ mutationFn: () => api.generate(gen), onSuccess: afterStart })
  const [showGen, setShowGen] = useState(false)
  const onGen = (e: FormEvent) => {
    e.preventDefault()
    generate.mutate()
  }

  return (
    <div className="page">
      <h2>Загрузка данных</h2>
      <div className="card">
        <h3>Файл NDJSON</h3>
        <div className="row gap wrap">
          <input ref={fileRef} type="file" accept=".ndjson,.jsonl,.json,.txt,application/x-ndjson" />
          <button
            className="primary"
            disabled={upload.isPending}
            onClick={() => {
              const f = fileRef.current?.files?.[0]
              if (f) upload.mutate(f)
            }}
          >
            {upload.isPending ? 'Отправка…' : 'Загрузить'}
          </button>
          <button onClick={() => setShowGen((s) => !s)}>Сгенерировать тестовые данные</button>
        </div>
        {upload.isError && <p className="error">{upload.error.message}</p>}
        <small className="muted">Интерфейс не блокируется: обработка идёт в фоне, сводка обновляется по ходу дела.</small>
      </div>

      {showGen && (
        <form className="card form" onSubmit={onGen}>
          <h3>Тестовые данные</h3>
          <div className="grid">
            <label>
              С даты
              <input type="date" value={gen.from} onChange={(e) => setGen({ ...gen, from: e.target.value })} required />
            </label>
            <label>
              По дату
              <input type="date" value={gen.to} onChange={(e) => setGen({ ...gen, to: e.target.value })} required />
            </label>
            <label>
              Часовой пояс
              <select value={gen.timezone} onChange={(e) => setGen({ ...gen, timezone: e.target.value })}>
                {timeZones(gen.timezone).map((z) => (
                  <option key={z}>{z}</option>
                ))}
              </select>
            </label>
            <label>
              Пресет
              <select value={gen.preset} onChange={(e) => setGen({ ...gen, preset: e.target.value })}>
                {PRESETS.map(([v, l]) => (
                  <option key={v} value={v}>
                    {l}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Доля пропусков: {Math.round(gen.gaps * 100)}%
              <input
                type="range"
                min={0}
                max={0.6}
                step={0.05}
                value={gen.gaps}
                onChange={(e) => setGen({ ...gen, gaps: Number(e.target.value) })}
              />
            </label>
          </div>
          {generate.isError && <p className="error">{generate.error.message}</p>}
          <button className="primary" disabled={generate.isPending || gen.from > gen.to}>
            Сгенерировать и загрузить
          </button>
        </form>
      )}

      <h3>Задачи</h3>
      {list.isPending && <p className="muted">Загрузка…</p>}
      {list.isError && <p className="error">{list.error.message}</p>}
      {list.data?.length === 0 && <p className="muted">Пока ничего не загружено</p>}
      <ul className="plain stack">
        {list.data?.map((u) => (
          <UploadRow key={u.id} u={u} live={progress[u.id]} onCancel={() => cancel.mutate(u.id)} />
        ))}
      </ul>
    </div>
  )
}
