import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'

type Kind = 'success' | 'error' | 'info'
interface Toast {
  id: number
  text: string
  kind: Kind
}
interface ToastApi {
  push: (text: string, kind?: Kind) => void
}
const Ctx = createContext<ToastApi>({ push: () => {} })
export const useToasts = () => useContext(Ctx)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([])
  const nextId = useRef(1)
  const push = useCallback((text: string, kind: Kind = 'info') => {
    const id = nextId.current++
    // не более 4 одновременно (при загрузке многих дней достижений будет поток)
    setItems((p) => [...p.slice(-3), { id, text, kind }])
    setTimeout(() => setItems((p) => p.filter((t) => t.id !== id)), 6000)
  }, [])
  const api = useMemo(() => ({ push }), [push])
  return (
    <Ctx.Provider value={api}>
      {children}
      <div className="toasts" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`} role="status">
            {t.text}
            <button
              className="toast-close"
              aria-label="Закрыть"
              onClick={() => setItems((p) => p.filter((x) => x.id !== t.id))}
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </Ctx.Provider>
  )
}
