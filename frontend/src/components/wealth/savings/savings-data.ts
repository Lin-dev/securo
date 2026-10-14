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
