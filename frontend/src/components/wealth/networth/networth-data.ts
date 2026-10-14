/**
 * Pure data-mapping helpers for the net worth tab (plan P2/P3, slice S2).
 * Kept separate from `networth-tab.tsx` so the bucket/sign/period logic is
 * testable without rendering recharts, and so this file can export plain
 * helpers without tripping `react-refresh/only-export-components` on the
 * component file.
 */
import { WEALTH_BUCKETS } from '@/types/wealth'
import type { WealthBucket, WealthNetWorthPoint, WealthPointFlag } from '@/types/wealth'

/** P3's liquid definition: cash + taxable + crypto, net of card debt. Loans
 *  and mortgages (`other_debt`) are not subtracted. */
export const LIQUID_BUCKETS: readonly WealthBucket[] = ['cash', 'taxable', 'crypto', 'card_debt']

export type NetWorthChartRow = Record<WealthBucket, number> & {
  date: string
  flags: WealthPointFlag[]
  /** True for points strictly before `synced_from` (or every point when
   *  `synced_from` is null) -- the "estimated from the ledger" period (P2). */
  isEstimatedPeriod: boolean
  /** The row's total, present only on/after `synced_from` so the line
   *  renders solid there (null elsewhere). */
  totalActual: number | null
  /** The row's total, present only on/before `synced_from` so the line
   *  renders dashed there (null elsewhere). The two overlap exactly at
   *  `synced_from` so the dashed and solid segments visually touch. */
  totalEstimated: number | null
  liquidActual: number | null
  liquidEstimated: number | null
}

/** Points on/after `syncedFrom` -- the chart's default period (P2). Every
 *  point counts as estimated when `syncedFrom` is null, so nothing qualifies
 *  as "synced" and this returns every point regardless of `includeEstimated`. */
export function visiblePoints(
  points: WealthNetWorthPoint[],
  syncedFrom: string | null,
  includeEstimated: boolean,
): WealthNetWorthPoint[] {
  if (includeEstimated || syncedFrom == null) return points
  return points.filter((point) => point.date >= syncedFrom)
}

/** Maps history points onto chart rows. Bucket amounts (including the two
 *  debt buckets, which the backend already sends as negative numbers) pass
 *  through unchanged -- this function never flips a sign -- so a
 *  `stackOffset="sign"` area chart stacks debt below the zero line. */
export function toChartRows(points: WealthNetWorthPoint[], syncedFrom: string | null): NetWorthChartRow[] {
  const sorted = [...points].sort((a, b) => a.date.localeCompare(b.date))

  return sorted.map((point) => {
    const beforeSync = syncedFrom == null || point.date < syncedFrom
    const atSyncStart = syncedFrom != null && point.date === syncedFrom

    const buckets = WEALTH_BUCKETS.reduce<Record<WealthBucket, number>>((acc, bucket) => {
      acc[bucket] = point.buckets[bucket]
      return acc
    }, {} as Record<WealthBucket, number>)

    return {
      ...buckets,
      date: point.date,
      flags: point.flags,
      isEstimatedPeriod: beforeSync,
      totalEstimated: beforeSync || atSyncStart ? point.total : null,
      totalActual: !beforeSync ? point.total : null,
      liquidEstimated: beforeSync || atSyncStart ? point.liquid : null,
      liquidActual: !beforeSync ? point.liquid : null,
    }
  })
}

/** True when any of `points` carries `flag` (plan P2's point flags). */
export function hasFlag(points: WealthNetWorthPoint[], flag: WealthPointFlag): boolean {
  return points.some((point) => point.flags.includes(flag))
}

export interface NetWorthDelta {
  amount: number
  fromDate: string
  toDate: string
}

/** The change from the earliest to the latest of `points`, on the total or
 *  the liquid series. Used for the chart's takeaway title. */
export function netWorthDelta(points: WealthNetWorthPoint[], liquidOnly: boolean): NetWorthDelta | null {
  if (points.length === 0) return null
  const sorted = [...points].sort((a, b) => a.date.localeCompare(b.date))
  const first = sorted[0]
  const last = sorted[sorted.length - 1]
  const firstValue = liquidOnly ? first.liquid : first.total
  const lastValue = liquidOnly ? last.liquid : last.total
  return { amount: lastValue - firstValue, fromDate: first.date, toDate: last.date }
}

/** Localized "Mon D" label (e.g. "Sep 30") for the takeaway title and the
 *  estimated-history note -- shorter than `lib/chart-format.ts`'s
 *  `formatMonth`, which carries the year. Kept local to this slice's files
 *  rather than added to the shared kit. */
export function formatDayLabel(isoDate: string, locale: string): string {
  const date = new Date(`${isoDate.slice(0, 10)}T00:00:00`)
  if (Number.isNaN(date.getTime())) return isoDate
  try {
    return date.toLocaleDateString(locale, { month: 'short', day: 'numeric' })
  } catch {
    return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  }
}
