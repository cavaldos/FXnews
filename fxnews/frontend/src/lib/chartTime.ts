import type { Time } from 'lightweight-charts'

// lightweight-charts works in UTC; format labels in the viewer's timezone instead
// (daily bars open at 00:00 UTC, so they stay on UTC to keep the right date).
export function makeTimeFormatters(daily: boolean) {
  const timeZone = daily ? 'UTC' : undefined
  const make = (o: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat('en-GB', { ...o, timeZone, hour12: false })
  const year = make({ year: 'numeric' })
  const month = make({ month: 'short' })
  const day = make({ day: 'numeric' })
  const hm = make({ hour: '2-digit', minute: '2-digit' })
  const hms = make({ hour: '2-digit', minute: '2-digit', second: '2-digit' })
  const crossIntraday = make({ day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
  const crossDaily = make({ day: '2-digit', month: 'short', year: 'numeric' })

  const toDate = (t: Time) => (typeof t === 'number' ? new Date(t * 1000) : null)

  return {
    // TickMarkType: 0 Year, 1 Month, 2 DayOfMonth, 3 Time, 4 TimeWithSeconds
    tickMarkFormatter: (t: Time, type: number): string | null => {
      const d = toDate(t)
      if (!d) return null
      switch (type) {
        case 0:
          return year.format(d)
        case 1:
          return month.format(d)
        case 2:
          return day.format(d)
        case 4:
          return hms.format(d)
        default:
          return hm.format(d)
      }
    },
    timeFormatter: (t: Time): string => {
      const d = toDate(t)
      if (!d) return String(t)
      return (daily ? crossDaily : crossIntraday).format(d)
    },
  }
}
