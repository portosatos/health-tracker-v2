import { describe, expect, it } from 'vitest'
import {
  NO_DATA,
  calcDeltaPct,
  formatDelta,
  formatMinutes,
  formatNumber,
  isMissing,
  percentOf,
} from './format'

describe('нет данных', () => {
  it('null не превращается в 0', () => {
    expect(formatNumber(null)).toBe(NO_DATA)
    expect(formatMinutes(null)).toBe(NO_DATA)
    expect(isMissing(null)).toBe(true)
    expect(isMissing(5, 'no_data')).toBe(true)
  })
  it('настоящий ноль остаётся нулём', () => {
    expect(formatNumber(0)).toBe('0')
    expect(isMissing(0, 'complete')).toBe(false)
  })
  it('минуты в часы', () => {
    expect(formatMinutes(425)).toBe('7 ч 05 мин')
    expect(formatMinutes(45)).toBe('45 мин')
  })
})

describe('сравнение', () => {
  it('рост и падение', () => {
    const up = formatDelta({ current: 110, previous: 100, delta_abs: 10, delta_pct: 10, comparable: true })
    expect(up.arrow).toBe('▲')
    expect(up.text).toBe('+10%')
    const down = formatDelta({ current: 75, previous: 100, delta_abs: -25, delta_pct: -25, comparable: true })
    expect(down.arrow).toBe('▼')
    expect(down.text).toBe('−25%')
  })
  it('несравнимо: показывается причина', () => {
    const v = formatDelta({
      current: 1,
      previous: null,
      delta_abs: null,
      delta_pct: null,
      comparable: false,
      reason: 'previous_no_data',
    })
    expect(v.tone).toBe('none')
    expect(v.text).toContain('предыдущ')
  })
  it('расчёт процентов', () => {
    expect(calcDeltaPct(150, 100)).toBe(50)
    expect(calcDeltaPct(5, 0)).toBeNull()
    expect(calcDeltaPct(null, 10)).toBeNull()
    expect(percentOf(4000, 8000)).toBe(50)
    expect(percentOf(null, 8000)).toBeNull()
  })
})
