/**
 * Month helpers for the shared chart kit, working in ISO `"YYYY-MM-DD"`
 * dates rather than the `"YYYY-MM"` strings `lib/month-utils.ts` uses for
 * the dashboard/transactions month stepper. Charts plot points on specific
 * ISO dates (a projection's monthly tick, a bullet bar's period), and need
 * to turn any one of those dates into the month window a drill-down link
 * should cover.
 */

/** First day of the calendar month containing `isoDate`, as `"YYYY-MM-DD"`. */
export function monthStart(isoDate: string): string {
  return `${isoDate.slice(0, 7)}-01`
}

/** Last day of the calendar month containing `isoDate`, as `"YYYY-MM-DD"`. */
export function monthEnd(isoDate: string): string {
  const [year, month] = isoDate.slice(0, 7).split('-').map(Number)
  const lastDay = new Date(year, month, 0).getDate()
  return `${isoDate.slice(0, 7)}-${String(lastDay).padStart(2, '0')}`
}

/**
 * The `{from, to}` window for the calendar month containing `isoDate` —
 * what a chart point's drill-down link needs so clicking a month bucket
 * opens Transactions scoped to that whole month, not just the one day.
 */
export function monthWindow(isoDate: string): { from: string; to: string } {
  return { from: monthStart(isoDate), to: monthEnd(isoDate) }
}

/**
 * Every month-start date (`"YYYY-MM-01"`) from the month containing
 * `fromIso` through the month containing `toIso`, inclusive. Used to build
 * a chart's x-axis categories from a date range, e.g. month-by-month
 * spending or a projection's monthly ticks.
 *
 * Returns an empty array when `toIso` falls before `fromIso`.
 */
export function monthRange(fromIso: string, toIso: string): string[] {
  const [fy, fm] = fromIso.slice(0, 7).split('-').map(Number)
  const [ty, tm] = toIso.slice(0, 7).split('-').map(Number)

  const months: string[] = []
  let year = fy
  let month = fm
  // Guard against a malformed pair running away; a chart's date range never
  // spans more than a few decades.
  let safety = 0
  while ((year < ty || (year === ty && month <= tm)) && safety < 1200) {
    months.push(`${year}-${String(month).padStart(2, '0')}-01`)
    month += 1
    if (month > 12) {
      month = 1
      year += 1
    }
    safety += 1
  }
  return months
}
