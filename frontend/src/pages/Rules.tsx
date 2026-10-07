import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'

export default function Rules() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['rules'], queryFn: api.rules })
  const toggle = useMutation({
    mutationFn: (v: { id: string; enabled: boolean }) => api.setRule(v.id, v.enabled),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['rules'] })
      void qc.invalidateQueries({ queryKey: ['recommendations'] })
    },
  })
  return (
    <div className="page">
      <h2>Правила</h2>
      <p className="muted">
        Правила срабатывают только по дням с полными данными. Отключение правила закрывает его открытые рекомендации
        (они остаются в истории).
      </p>
      {q.isPending && <p className="muted">Загрузка…</p>}
      {q.isError && <p className="error">{q.error.message}</p>}
      {toggle.isError && <p className="error">{toggle.error.message}</p>}
      <ul className="plain stack">
        {q.data?.map((r) => (
          <li key={r.id} className="card row between gap">
            <div>
              <strong>{r.title}</strong> {r.open_count > 0 && <span className="badge badge-warn">открыто: {r.open_count}</span>}
              <p>{r.description}</p>
              {Object.keys(r.params ?? {}).length > 0 && (
                <small className="muted">
                  Параметры:{' '}
                  {Object.entries(r.params)
                    .map(([k, v]) => `${k} = ${String(v)}`)
                    .join(', ')}
                </small>
              )}
            </div>
            <label className="switch">
              <input
                type="checkbox"
                checked={r.enabled}
                disabled={toggle.isPending}
                onChange={(e) => toggle.mutate({ id: r.id, enabled: e.target.checked })}
              />
              <span>{r.enabled ? 'Включено' : 'Выключено'}</span>
            </label>
          </li>
        ))}
      </ul>
    </div>
  )
}
