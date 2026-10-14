import { todayInTimezone } from '@/lib/date-utils'

export interface BudgetPaceToday {
  /** `"YYYY-MM"` */
  month: string
  /** 1-31, the day-of-month "today" counts as. */
  dayOfMonth: number
}

/**
 * "Today" for the dashboard's budget-pace math (days elapsed this month,
 * and whether the selected month even is the current one), resolved in the
 * workspace's own timezone when one is known (plan S3 bug fix).
 *
 * The browser's own clock can land on a different calendar day — and, right
 * around a month boundary, a different month — than the workspace's books
 * are kept in. `monthLastDay`-sized pace figures computed from the wrong
 * "today" either skip the first day's pace entirely or treat an in-progress
 * month as though it had already finished. Falls back to the browser's
 * clock, exactly as `todayInTimezone` itself does, when no workspace
 * timezone is known yet.
 */
export function resolveBudgetPaceToday(
  timeZone: string | null | undefined,
  now = new Date(),
): BudgetPaceToday {
  const iso = todayInTimezone(timeZone, now)
  return { month: iso.slice(0, 7), dayOfMonth: Number(iso.slice(8, 10)) }
}
