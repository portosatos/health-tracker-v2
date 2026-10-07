import { Navigate, Route, Routes } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api, ApiError } from './api'
import { EventsProvider } from './sse'
import Layout from './components/Layout'
import AuthPage from './pages/AuthPage'
import Dashboard from './pages/Dashboard'
import Uploads from './pages/Uploads'
import Goals from './pages/Goals'
import Recommendations from './pages/Recommendations'
import Rules from './pages/Rules'
import Mail from './pages/Mail'
import SettingsPage from './pages/SettingsPage'

export default function App() {
  const me = useQuery({
    queryKey: ['me'],
    queryFn: async () => {
      try {
        return await api.me()
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null
        throw e
      }
    },
  })

  if (me.isPending) return <div className="center muted">Загрузка…</div>
  if (me.isError)
    return (
      <div className="center">
        <p>Не удалось связаться с сервером.</p>
        <button onClick={() => void me.refetch()}>Повторить</button>
      </div>
    )
  if (!me.data) return <AuthPage />

  return (
    <EventsProvider>
      <Layout me={me.data}>
        <Routes>
          <Route path="/" element={<Dashboard me={me.data} />} />
          <Route path="/uploads" element={<Uploads me={me.data} />} />
          <Route path="/goals" element={<Goals me={me.data} />} />
          <Route path="/recommendations" element={<Recommendations />} />
          <Route path="/rules" element={<Rules />} />
          <Route path="/mail" element={<Mail me={me.data} />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Layout>
    </EventsProvider>
  )
}
