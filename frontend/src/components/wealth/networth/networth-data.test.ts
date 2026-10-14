import { describe, expect, it } from 'vitest'

import {
  formatDayLabel,
  hasFlag,
  netWorthDelta,
  toChartRows,
  visiblePoints,
} from './networth-data'
import type { WealthNetWorthPoint } from '@/types/wealth'

function buckets(over: Partial<Record<string, number>> = {}): WealthNetWorthPoint['buckets'] {
  return {
    cash: 0,
    taxable: 0,
    retirement_pretax: 0,
    retirement_roth: 0,
    retirement_hsa: 0,
    crypto: 0,
    property_other: 0,
    card_debt: 0,
    other_debt: 0,
    ...over,
  }
}

const points: WealthNetWorthPoint[] = [
  {
    date: '2026-08-31',
    total: 400000,
    liquid: 150000,
    buckets: buckets({ cash: 50000, taxable: 100000, card_debt: -4000 }),
    flags: ['estimated'],
  },
  {
    date: '2026-09-30',
    total: 490000,
    liquid: 195000,
    buckets: buckets({ cash: 60000, taxable: 140000, crypto: 5000, card_debt: -5000 }),
    flags: [],
  },
  {
    date: '2026-10-14',
    total: 500000,
    liquid: 200000,
    buckets: buckets({ cash: 62000, taxable: 143000, crypto: 5000, card_debt: -5000 }),
    flags: ['cash_flat'],
  },
]

const syncedFrom = '2026-09-30'

describe('visiblePoints', () => {
  it('keeps only points on/after syncedFrom when estimated history is excluded', () => {
    const visible = visiblePoints(points, syncedFrom, false)
    expect(visible.map((p) => p.date)).toEqual(['2026-09-30', '2026-10-14'])
  })

  it('includes every point when estimated history is included', () => {
    const visible = visiblePoints(points, syncedFrom, true)
    expect(visible).toHaveLength(3)
  })

  it('includes every point when syncedFrom is null, regardless of the toggle', () => {
    expect(visiblePoints(points, null, false)).toHaveLength(3)
    expect(visiblePoints(points, null, true)).toHaveLength(3)
  })
})

describe('toChartRows', () => {
  it('passes debt bucket amounts through as negatives, never flipping the sign', () => {
    const rows = toChartRows(points, syncedFrom)
    for (const row of rows) {
      expect(row.card_debt).toBeLessThanOrEqual(0)
    }
    expect(rows.find((r) => r.date === '2026-09-30')?.card_debt).toBe(-5000)
  })

  it('sorts rows by date', () => {
    const shuffled = [points[2], points[0], points[1]]
    const rows = toChartRows(shuffled, syncedFrom)
    expect(rows.map((r) => r.date)).toEqual(['2026-08-31', '2026-09-30', '2026-10-14'])
  })

  it('marks points before syncedFrom as the estimated period', () => {
    const rows = toChartRows(points, syncedFrom)
    expect(rows.find((r) => r.date === '2026-08-31')?.isEstimatedPeriod).toBe(true)
    expect(rows.find((r) => r.date === '2026-10-14')?.isEstimatedPeriod).toBe(false)
  })

  it('marks every point as the estimated period when syncedFrom is null', () => {
    const rows = toChartRows(points, null)
    expect(rows.every((r) => r.isEstimatedPeriod)).toBe(true)
  })

  it('splits the total line so actual and estimated overlap exactly at syncedFrom', () => {
    const rows = toChartRows(points, syncedFrom)
    const before = rows.find((r) => r.date === '2026-08-31')!
    const atSync = rows.find((r) => r.date === '2026-09-30')!
    const after = rows.find((r) => r.date === '2026-10-14')!

    expect(before.totalEstimated).toBe(400000)
    expect(before.totalActual).toBeNull()

    expect(atSync.totalEstimated).toBe(490000)
    expect(atSync.totalActual).toBe(490000)

    expect(after.totalEstimated).toBeNull()
    expect(after.totalActual).toBe(500000)
  })

  it('does the same split for the liquid line', () => {
    const rows = toChartRows(points, syncedFrom)
    const before = rows.find((r) => r.date === '2026-08-31')!
    const after = rows.find((r) => r.date === '2026-10-14')!

    expect(before.liquidEstimated).toBe(150000)
    expect(before.liquidActual).toBeNull()
    expect(after.liquidEstimated).toBeNull()
    expect(after.liquidActual).toBe(200000)
  })
})

describe('hasFlag', () => {
  it('finds a flag present on any point', () => {
    expect(hasFlag(points, 'cash_flat')).toBe(true)
    expect(hasFlag(points, 'estimated')).toBe(true)
  })

  it('returns false when no point carries the flag', () => {
    expect(hasFlag(points, 'no_holdings')).toBe(false)
  })

  it('returns false for an empty list', () => {
    expect(hasFlag([], 'cash_flat')).toBe(false)
  })
})

describe('netWorthDelta', () => {
  it('computes the change on total from the earliest to the latest point', () => {
    const delta = netWorthDelta(points, false)
    expect(delta).toEqual({ amount: 100000, fromDate: '2026-08-31', toDate: '2026-10-14' })
  })

  it('computes the change on liquid when liquidOnly is true', () => {
    const delta = netWorthDelta(points, true)
    expect(delta).toEqual({ amount: 50000, fromDate: '2026-08-31', toDate: '2026-10-14' })
  })

  it('returns null for an empty list', () => {
    expect(netWorthDelta([], false)).toBeNull()
  })

  it('handles a single point as a zero change', () => {
    const delta = netWorthDelta([points[0]], false)
    expect(delta).toEqual({ amount: 0, fromDate: '2026-08-31', toDate: '2026-08-31' })
  })
})

describe('formatDayLabel', () => {
  it('formats an ISO date as "Mon D"', () => {
    expect(formatDayLabel('2026-09-30', 'en-US')).toBe('Sep 30')
  })

  it('falls back to the raw string for an invalid date', () => {
    expect(formatDayLabel('not-a-date', 'en-US')).toBe('not-a-date')
  })
})
