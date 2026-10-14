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

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

/** `"YYYY-MM-DD"` for a plain (non-UTC) `Date`, built from its local parts
 * rather than `toISOString()` so a midnight-local date never rolls back a
 * day under a negative-offset timezone. */
function formatLocalDate(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

/** Monday of the ISO-8601 week `isoWeek` of `isoYear` (the week containing
 * that year's January 4th is week 1). Standard "week date to calendar date"
 * conversion: start from Jan 1 + (week-1)*7 days, then walk back/forward to
 * the Monday of ISO week it actually falls in. */
function isoWeekMonday(isoYear: number, isoWeek: number): Date {
  const simple = new Date(isoYear, 0, 1 + (isoWeek - 1) * 7)
  const dow = simple.getDay() // 0 = Sunday .. 6 = Saturday
  const monday = new Date(simple)
  if (dow <= 4) monday.setDate(simple.getDate() - dow + 1)
  else monday.setDate(simple.getDate() + 8 - dow)
  return monday
}

/**
 * The `{from, to}` window (Monday through Sunday) for an ISO week label
 * (`"YYYY-Www"`, e.g. `"2026-W29"` -- the format `_format_date_label` emits
 * for `interval="weekly"`, from Python's `date.isocalendar()`).
 */
export function weekWindow(label: string): { from: string; to: string } {
  const [yearStr, weekStr] = label.split('-W')
  const monday = isoWeekMonday(Number(yearStr), Number(weekStr))
  const sunday = new Date(monday)
  sunday.setDate(monday.getDate() + 6)
  return { from: formatLocalDate(monday), to: formatLocalDate(sunday) }
}

/** The `{from, to}` window (January 1 through December 31) for a bare year
 * label (`"YYYY"`, what `_format_date_label` emits for `interval="yearly"`). */
export function yearWindow(label: string): { from: string; to: string } {
  return { from: `${label}-01-01`, to: `${label}-12-31` }
}

export type ReportChartInterval = 'daily' | 'weekly' | 'monthly' | 'yearly'

/**
 * The `{from, to}` window a report chart's period LABEL covers, given the
 * report's own bucketing `interval` (`report_service.py`'s
 * `_format_date_label`): daily buckets are labelled by the ISO date itself
 * (a single day), weekly by `"YYYY-Www"`, monthly by `"YYYY-MM"`, yearly by
 * `"YYYY"`. A chart click only knows the label and the interval it was
 * fetched under -- never which calendar unit produced it -- so every
 * period-bar/point click on the Reports page should go through this rather
 * than re-deriving a window from the label's own string shape.
 */
export function periodWindow(label: string, interval: string): { from: string; to: string } {
  if (interval === 'weekly') return weekWindow(label)
  if (interval === 'yearly') return yearWindow(label)
  if (interval === 'monthly') return monthWindow(label)
  return { from: label, to: label }
}

/**
 * The window immediately BEFORE `{from, to}`, with the same length in days,
 * ending the day before `from` -- what the "compare with previous period"
 * overlay fetches with the same report call, explicit `start_date`/
 * `end_date` (plan S6). Example: `Jul 1 - Sep 30` (92 days) shifts back to
 * `Mar 31 - Jun 30` (92 days ending the day before Jul 1).
 */
export function previousWindow(from: string, to: string): { from: string; to: string } {
  const fromDate = new Date(`${from}T00:00:00`)
  const toDate = new Date(`${to}T00:00:00`)
  const spanDays = Math.round((toDate.getTime() - fromDate.getTime()) / 86_400_000) + 1

  const prevTo = new Date(fromDate)
  prevTo.setDate(prevTo.getDate() - 1)
  const prevFrom = new Date(prevTo)
  prevFrom.setDate(prevFrom.getDate() - (spanDays - 1))

  return { from: formatLocalDate(prevFrom), to: formatLocalDate(prevTo) }
}
