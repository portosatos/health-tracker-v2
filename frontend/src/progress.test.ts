import { describe, expect, it } from 'vitest'
import { computePercent, mergeProgress, type ProgressState } from './progress'

const base: ProgressState = {
  upload_id: 1,
  status: 'processing',
  lines_done: 100,
  lines_total: 1000,
  percent: 10,
  rows_inserted: 100,
  rows_updated: 0,
  rows_duplicate: 0,
  rows_rejected: 0,
}

describe('mergeProgress', () => {
  it('прогресс не убывает при запоздавшем событии', () => {
    const r = mergeProgress({ ...base, lines_done: 500, percent: 50 }, base)
    expect(r.lines_done).toBe(500)
  })
  it('двигается вперёд', () => {
    expect(mergeProgress(base, { ...base, lines_done: 300, percent: 30 }).percent).toBe(30)
  })
  it('терминальный статус не откатывается', () => {
    const done = { ...base, status: 'done' as const, lines_done: 1000, percent: 100 }
    expect(mergeProgress(done, base).status).toBe('done')
  })
  it('без предыдущего берёт новое', () => {
    expect(mergeProgress(undefined, base)).toBe(base)
  })
})

describe('computePercent', () => {
  it('граничные случаи', () => {
    expect(computePercent(5, null)).toBe(0)
    expect(computePercent(50, 100)).toBe(50)
    expect(computePercent(200, 100)).toBe(100)
  })
})
