import type { RangeLinePoint } from '@/components/charts/RangeLines'
import type { WealthNetWorthPoint, WealthProjectionPathPoint } from '@/types/wealth'

/**
 * Contract note (plan P8, R1 S1): `WealthProjection.scenarios.*.path[].balance`
 * is the *invested* balance only (property/vehicles excluded, FX applied —
 * plan P5). Charting **net worth** against the $1M goal means adding
 * `inputs.non_invested` to every point, since the goal and the hero both
 * talk about net worth, not just the invested piece.
 */
export function projectionPathToNetWorth(
  path: WealthProjectionPathPoint[],
  nonInvested: number,
): RangeLinePoint[] {
  return path.map((point) => ({ date: point.month, value: point.balance + nonInvested }))
}

/**
 * The actual history line comes from a different endpoint
 * (`netWorthHistory(...).points[].total`), which is already net worth
 * (every bucket), unlike the projection paths above.
 *
 * Points earlier than `from` are dropped. Before the accounts existed (or
 * before they were synced), history points are back-computed estimates
 * that can read near $0 and make the line look broken when it jumps up at
 * the real start date — so the caller passes `netWorthHistory.synced_from`
 * (falling back to `summary.coverage.from_date` when that's null) and only
 * the real, synced window gets charted. `from: null` charts every point.
 */
export function historyToRangePoints(points: WealthNetWorthPoint[], from: string | null): RangeLinePoint[] {
  return points
    .filter((point) => from == null || point.date >= from)
    .map((point) => ({ date: point.date, value: point.total }))
}

export interface ProjectionTableRow {
  id: string
  month: string
  low: number | null
  base: number | null
  high: number | null
}

/** Merges the three scenario paths into one row per month, for the chart's
 *  table fallback (plan P8: every chart has a table fallback). */
export function buildProjectionTableRows(
  low: RangeLinePoint[],
  base: RangeLinePoint[],
  high: RangeLinePoint[],
): ProjectionTableRow[] {
  const byDate = new Map<string, ProjectionTableRow>()
  const put = (points: RangeLinePoint[], key: 'low' | 'base' | 'high') => {
    for (const point of points) {
      const row = byDate.get(point.date) ?? { id: point.date, month: point.date, low: null, base: null, high: null }
      row[key] = point.value
      byDate.set(point.date, row)
    }
  }
  put(low, 'low')
  put(base, 'base')
  put(high, 'high')
  return [...byDate.values()].sort((a, b) => a.month.localeCompare(b.month))
}
