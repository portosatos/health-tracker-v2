const FALLBACK = [
  'UTC',
  'Europe/Kaliningrad',
  'Europe/Moscow',
  'Europe/Samara',
  'Asia/Yekaterinburg',
  'Asia/Omsk',
  'Asia/Novosibirsk',
  'Asia/Krasnoyarsk',
  'Asia/Irkutsk',
  'Asia/Yakutsk',
  'Asia/Vladivostok',
  'Asia/Magadan',
  'Asia/Kamchatka',
  'Europe/London',
  'Europe/Berlin',
  'Europe/Kyiv',
  'Europe/Istanbul',
  'Asia/Dubai',
  'Asia/Tashkent',
  'Asia/Almaty',
  'Asia/Bangkok',
  'Asia/Shanghai',
  'Asia/Tokyo',
  'America/New_York',
  'America/Chicago',
  'America/Los_Angeles',
]

/** IANA-список из браузера (Intl.supportedValuesOf); при отсутствии — короткий запасной список. */
export function timeZones(include?: string): string[] {
  let list: string[] = FALLBACK
  try {
    const l = Intl.supportedValuesOf('timeZone')
    if (l.length) list = l.includes('UTC') ? l : ['UTC', ...l]
  } catch {
    /* старый браузер */
  }
  return include && !list.includes(include) ? [include, ...list] : list
}
