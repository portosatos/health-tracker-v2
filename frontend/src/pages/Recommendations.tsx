import { useState } from 'react'
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type Recommendation } from '../api'
import { formatDateTime } from '../format'

const REASONS: Record<string, string> = {
  auto_cleared: 'ситуация устранена',
  rule_disabled: 'правило отключено',
  expired: 'истёк срок',
}

function RecCard({ r }: { r: Recommendation }) {
  const qc = useQueryClient()
  const fb = useMutation({
    mutationFn: (v: 'helpful' | 'not_helpful' | null) => api.feedback(r.id, v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['recommendations'] }),
  })
  const toggle = (v: 'helpful' | 'not_helpful') => fb.mutate(r.feedback === v ? null : v)
  return (
    <li className="card rec">
      <div className="row between wrap">
        <strong>{r.title}</strong>
        <span className={r.status === 'open' ? 'badge badge-warn' : 'badge'}>
          {r.status === 'open' ? 'открыта' : 'закрыта'}
        </span>
      </div>
      <p>{r.body}</p>
      <small className="muted">
        Открыта {formatDateTime(r.opened_at)}
        {r.status === 'resolved' && (
          <>
            {' · закрыта '}
            {formatDateTime(r.resolved_at)}
            {r.resolution_reason && ` (${REASONS[r.resolution_reason] ?? r.resolution_reason})`}
          </>
        )}
      </small>
      <details>
        <summary>Основание</summary>
        <pre>{JSON.stringify(r.evidence, null, 2)}</pre>
      </details>
      <div className="row gap">
        <button className={r.feedback === 'helpful' ? 'on' : ''} disabled={fb.isPending} onClick={() => toggle('helpful')}>
          👍 Полезно
        </button>
        <button
          className={r.feedback === 'not_helpful' ? 'on' : ''}
          disabled={fb.isPending}
          onClick={() => toggle('not_helpful')}
        >
          👎 Не полезно
        </button>
      </div>
      {fb.isError && <p className="error">{fb.error.message}</p>}
    </li>
  )
}

function List({ status }: { status: 'open' | 'resolved' }) {
  const q = useInfiniteQuery({
    queryKey: ['recommendations', status],
    queryFn: ({ pageParam }) => api.recommendations(status, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  })
  const items = q.data?.pages.flatMap((p) => p.items) ?? []
  return (
    <>
      {q.isPending && <p className="muted">Загрузка…</p>}
      {q.isError && <p className="error">{q.error.message}</p>}
      {q.isSuccess && items.length === 0 && (
        <p className="muted">{status === 'open' ? 'Активных рекомендаций нет' : 'История пуста'}</p>
      )}
      <ul className="plain stack">
        {items.map((r) => (
          <RecCard key={r.id} r={r} />
        ))}
      </ul>
      {q.hasNextPage && (
        <button disabled={q.isFetchingNextPage} onClick={() => void q.fetchNextPage()}>
          Показать ещё
        </button>
      )}
    </>
  )
}

export default function Recommendations() {
  const [tab, setTab] = useState<'open' | 'resolved'>('open')
  return (
    <div className="page">
      <h2>Рекомендации</h2>
      <div className="seg">
        <button className={tab === 'open' ? 'on' : ''} onClick={() => setTab('open')}>
          Активные
        </button>
        <button className={tab === 'resolved' ? 'on' : ''} onClick={() => setTab('resolved')}>
          История
        </button>
      </div>
      <List key={tab} status={tab} />
    </div>
  )
}
