import { useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'

export default function AuthPage() {
  const qc = useQueryClient()
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [login, setLogin] = useState('')
  const [password, setPassword] = useState('')
  const m = useMutation({
    mutationFn: () => (mode === 'login' ? api.login(login, password) : api.register(login, password)),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['me'] }),
  })
  const tooShort = mode === 'register' && (login.trim().length < 3 || password.length < 8)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!tooShort) m.mutate()
  }
  return (
    <div className="center">
      <form className="card auth" onSubmit={submit}>
        <h1>Трекер здоровья</h1>
        <div className="tabs">
          <button type="button" className={mode === 'login' ? 'tab on' : 'tab'} onClick={() => setMode('login')}>
            Вход
          </button>
          <button type="button" className={mode === 'register' ? 'tab on' : 'tab'} onClick={() => setMode('register')}>
            Регистрация
          </button>
        </div>
        <label>
          Логин
          <input value={login} onChange={(e) => setLogin(e.target.value)} autoComplete="username" required />
        </label>
        <label>
          Пароль
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            required
          />
        </label>
        {mode === 'register' && <small className="muted">Логин от 3 символов, пароль от 8.</small>}
        {m.isError && <p className="error">{m.error.message}</p>}
        <button className="primary" disabled={m.isPending || tooShort}>
          {mode === 'login' ? 'Войти' : 'Создать аккаунт'}
        </button>
      </form>
    </div>
  )
}
