import { useState, type ReactNode } from 'react'
import { NavLink } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type Me } from '../api'
import { useEvents } from '../sse'
import { formatDateTime } from '../format'

const NAV = [
  ['/', 'Сводка'],
  ['/uploads', 'Загрузка данных'],
  ['/goals', 'Цели'],
  ['/recommendations', 'Рекомендации'],
  ['/rules', 'Правила'],
  ['/mail', 'Почта'],
  ['/settings', 'Настройки'],
] as const

function Bell({ tz }: { tz: string }) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const list = useQuery({ queryKey: ['notifications'], queryFn: () => api.notifications(true, 50) })
  const readAll = useMutation({
    mutationFn: api.readAllNotifications,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['notifications'] }),
  })
  const readOne = useMutation({
    mutationFn: api.readNotification,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['notifications'] }),
  })
  const items = list.data ?? []
  return (
    <div className="bell">
      <button className="icon-btn" onClick={() => setOpen((o) => !o)} aria-label="Уведомления">
        🔔{items.length > 0 && <span className="bell-count">{items.length > 99 ? '99+' : items.length}</span>}
      </button>
      {open && (
        <div className="bell-panel card">
          <div className="row between">
            <strong>Уведомления</strong>
            {items.length > 0 && (
              <button className="link" onClick={() => readAll.mutate()}>
                Прочитать все
              </button>
            )}
          </div>
          {items.length === 0 && <p className="muted">Новых уведомлений нет</p>}
          <ul className="plain">
            {items.map((n) => (
              <li key={n.id} className="bell-item">
                <div>
                  <div>{describe(n.kind, n.payload)}</div>
                  <small className="muted">{formatDateTime(n.created_at, tz)}</small>
                </div>
                <button className="link" onClick={() => readOne.mutate(n.id)}>
                  ✓
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function describe(kind: string, p: Record<string, unknown>): string {
  if (kind === 'goal_achieved') {
    const what = p.goal_kind === 'steps_per_day' ? 'шаги' : 'сон'
    return `Цель достигнута: ${what}, ${String(p.date ?? p.local_date ?? '')}`
  }
  if (kind === 'recommendation') return `Новая рекомендация: ${String(p.title ?? '')}`
  return kind
}

export default function Layout({ me, children }: { me: Me; children: ReactNode }) {
  const qc = useQueryClient()
  const { connected } = useEvents()
  const logout = useMutation({
    mutationFn: api.logout,
    onSuccess: () => {
      qc.clear()
      void qc.invalidateQueries()
      window.location.assign('/')
    },
  })
  return (
    <div className="shell">
      <header className="topbar">
        <strong className="brand">Трекер здоровья</strong>
        <nav className="nav">
          {NAV.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === '/'}>
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="top-right">
          <span className={connected ? 'dot dot-on' : 'dot'} title={connected ? 'Онлайн' : 'Нет связи с сервером событий'} />
          <Bell tz={me.timezone} />
          <span className="muted who">{me.login}</span>
          <button className="link" onClick={() => logout.mutate()}>
            Выйти
          </button>
        </div>
      </header>
      <main className="content">{children}</main>
    </div>
  )
}
