import { describe, expect, it } from 'vitest'
import { addDays, weekStart } from './dates'

describe('dates', () => {
  it('addDays через границу месяца', () => {
    expect(addDays('2026-03-01', -1)).toBe('2026-02-28')
  })
  it('понедельник недели', () => {
    expect(weekStart('2026-03-08')).toBe('2026-03-02')
    expect(weekStart('2026-03-02')).toBe('2026-03-02')
  })
})
