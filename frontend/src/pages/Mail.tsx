import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, ApiError, type Me, type OutboxItem } from '../api'
import { formatDateTime } from '../format'

const STATUS: Record<OutboxItem['status'], string> = {
  pending: 'ожидает отправки',
  sending: 'отправляется',
  sent: 'отправлено',
  failed: 'ошибка, будет повтор',
  dead: 'не доставлено',
  cancelled: 'отменено',
}

function statusText(m: OutboxItem, tz: string): string {
  if (m.status === 'pending' && m.send_after && new Date(m.send_after) > new Date()) {
    const why = m.defer_reason === 'quiet_hours' ? ' из-за тихих часов' : m.defer_reason === 'hold' ? ' (склейка писем)' : ''
    return `отложено до ${formatDateTime(m.send_after, tz)}${why}`
  }
  if (m.status === 'sent') return `отправлено ${formatDateTime(m.sent_at, tz)}`
  return STATUS[m.status]
}

export default function Mail({ me }: { me: Me }) {
  const outbox = useQuery({ queryKey: ['outbox'], queryFn: () => api.outbox(50) })
  // /dev/mailbox доступен только в dev/test — 404/403 не считаем ошибкой экрана
  const mailbox = useQuery({
    queryKey: ['mailbox'],
    queryFn: () => api.mailbox(50),
    retry: false,
  })
  const [selOut, setSelOut] = useState<number | null>(null)
  const [selBox, setSelBox] = useState<number | null>(null)
  const out = outbox.data?.find((m) => m.id === selOut)
  const box = mailbox.data?.find((m) => m.id === selBox)
  const boxUnavailable = mailbox.error instanceof ApiError && [403, 404].includes(mailbox.error.status)

  return (
    <div className="page">
      <h2>Почта</h2>
      <h3>Исходящие</h3>
      {outbox.isPending && <p className="muted">Загрузка…</p>}
      {outbox.isError && <p className="error">{outbox.error.message}</p>}
      {outbox.data?.length === 0 && <p className="muted">Писем пока нет</p>}
      <ul className="plain stack">
        {outbox.data?.map((m) => (
          <li key={m.id} className="card">
            <div className="row between wrap">
              <button className="link" onClick={() => setSelOut(selOut === m.id ? null : m.id)}>
                {m.subject}
              </button>
              <span className={`badge badge-mail-${m.status}`}>{statusText(m, me.timezone)}</span>
            </div>
            <small className="muted">
              Рекомендаций: {m.recommendation_ids.length}
              {m.attempts > 0 && ` · попыток: ${m.attempts}`}
            </small>
            {m.last_error && <p className="error">{m.last_error}</p>}
            {out?.id === m.id && (
              <div className="mailview">
                {m.body_text ? <pre>{m.body_text}</pre> : <p className="muted">Текст письма см. в разделе «Доставленные» ниже.</p>}
              </div>
            )}
          </li>
        ))}
      </ul>

      <h3>Доставленные (тестовый ящик)</h3>
      {mailbox.isPending && <p className="muted">Загрузка…</p>}
      {boxUnavailable && <p className="muted">Тестовый ящик недоступен в этом окружении.</p>}
      {mailbox.isError && !boxUnavailable && <p className="error">{mailbox.error.message}</p>}
      {mailbox.data?.length === 0 && <p className="muted">Доставленных писем нет</p>}
      <ul className="plain stack">
        {mailbox.data?.map((m) => (
          <li key={m.id} className="card">
            <div className="row between wrap">
              <button className="link" onClick={() => setSelBox(selBox === m.id ? null : m.id)}>
                {m.subject ?? '(без темы)'}
              </button>
              <small className="muted">
                {m.rcpt} · {formatDateTime(m.received_at, me.timezone)}
              </small>
            </div>
            {box?.id === m.id && (
              <div className="mailview">
                <pre>{m.body_text}</pre>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
