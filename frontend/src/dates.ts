// Работа с календарными датами YYYY-MM-DD без часовых поясов (всё в UTC-арифметике).
export function parseDate(s: string): Date {
  const [y, m, d] = s.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, d))
}
export function toDateStr(d: Date): string {
  return d.toISOString().slice(0, 10)
}
export function addDays(s: string, n: number): string {
  const d = parseDate(s)
  d.setUTCDate(d.getUTCDate() + n)
  return toDateStr(d)
}
/** Понедельник ISO-недели, содержащей дату. */
export function weekStart(s: string): string {
  const dow = (parseDate(s).getUTCDay() + 6) % 7
  return addDays(s, -dow)
}
export function todayIn(tz?: string): string {
  try {
    return new Intl.DateTimeFormat('en-CA', {
      timeZone: tz,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(new Date())
  } catch {
    return toDateStr(new Date())
  }
}
const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
export function formatDay(s: string): string {
  const d = parseDate(s)
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`
}
export function formatDayLong(s: string): string {
  const d = parseDate(s)
  const wd = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'][d.getUTCDay()]
  return `${wd}, ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`
}
