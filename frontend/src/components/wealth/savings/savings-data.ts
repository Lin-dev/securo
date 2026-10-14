/**
 * Pure data-mapping helpers for the savings tab (plan P1, slice S2). Kept
 * separate from `savings-tab.tsx` so the toggle/rolling-rate logic is
 * testable without rendering recharts, and so this file can export plain
 * helpers without tripping `react-refresh/only-export-components` on the
 * component file.
 */
import type { WealthSavingsMonth } from '@/types/wealth'

/** Reference bands the chart draws as faint horizontal lines (plan S2). */
export const SAVINGS_RATE_BANDS = [0.2, 0.35, 0.5] as const

/** The chart's default months: in-coverage months, plus the current
 *  (possibly not-yet-in-coverage) partial month. On real data, a handful of
 *  stray rows dated before `coverage_window` starts carry almost no income
 *  and produce huge, meaningless rates that would otherwise dominate the
 *  chart -- so they stay hidden until "Show earlier months" is on. */
export function visibleMonths(months: WealthSavingsMonth[], showEarlier: boolean): WealthSavingsMonth[] {
  if (showEarlier) return months
  return months.filter((month) => month.in_coverage || month.partial)
}

export interface SavingsChartRow {
  month: string
  /** `rate` (P1, "incl. payroll contributions") or `cash_rate`, whichever the toggle selects. */
  value: number | null
  rolling3m: number | null
  partial: boolean
  inCoverage: boolean
}

/** Maps months onto chart rows, picking the active rate per the toggle. */
export function toChartRows(months: WealthSavingsMonth[], useCashRate: boolean): SavingsChartRow[] {
  const sorted = [...months].sort((a, b) => a.month.localeCompare(b.month))
  return sorted.map((month) => ({
    month: month.month,
    value: useCashRate ? month.cash_rate : month.rate,
    rolling3m: month.rolling_3m,
    partial: month.partial,
    inCoverage: month.in_coverage,
  }))
}

export interface LatestRollingRate {
  value: number
  month: string
}

/** The most recent month with a non-null `rolling_3m`, for the chart's
 *  takeaway title ("Your 3-month savings rate is 31%"). `rolling_3m` is a
 *  single series in the contract -- it does not split by the incl./cash-only
 *  toggle -- so the title states it plainly rather than naming a basis. */
export function latestRollingRate(months: WealthSavingsMonth[]): LatestRollingRate | null {
  const sorted = [...months].sort((a, b) => b.month.localeCompare(a.month))
  const found = sorted.find((month) => month.rolling_3m != null)
  return found ? { value: found.rolling_3m as number, month: found.month } : null
}

/** The most recent month's own selected rate, used as the title's fallback
 *  when no month has a rolling average yet (too little history). */
export function latestMonthRate(months: WealthSavingsMonth[], useCashRate: boolean): number | null {
  if (months.length === 0) return null
  const sorted = [...months].sort((a, b) => b.month.localeCompare(a.month))
  const latest = sorted[0]
  return useCashRate ? latest.cash_rate : latest.rate
}

/** The y-axis's hard ceiling/floor: one wild month (a stray pre-coverage row,
 *  or a one-off transfer) must not flatten every other bar into a sliver. */
export const SAVINGS_RATE_CLIP_BOUND = 1

/** True once a rate falls outside [-100%, 100%] -- the chart clips its bar at
 *  the domain edge and marks it, rather than letting it stretch the axis. */
export function isClipped(value: number | null): boolean {
  return value != null && Math.abs(value) > SAVINGS_RATE_CLIP_BOUND
}

/** The y-axis domain's lower bound: always includes 0, and never drops below
 *  -100% however far a wild month's `dataMin` runs -- the visible data's own
 *  range when that is narrower than the ±100% ceiling (recharts calls this
 *  with the real `dataMin` across the chart's rows; paired with
 *  `allowDataOverflow` on `<YAxis>` so a clamped domain actually clips
 *  instead of recharts silently re-expanding it to fit the outlier). */
export function domainMin(dataMin: number): number {
  return Math.max(-SAVINGS_RATE_CLIP_BOUND, Math.min(0, dataMin))
}

/** The y-axis domain's upper bound: at least enough headroom to clear the
 *  50% band, never above 100%. */
export function domainMax(dataMax: number): number {
  return Math.min(SAVINGS_RATE_CLIP_BOUND, Math.max(0.55, dataMax) * 1.1)
}
