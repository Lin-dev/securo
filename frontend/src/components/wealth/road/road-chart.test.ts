import { describe, expect, it } from 'vitest'

import {
  buildProjectionTableRows,
  historyToRangePoints,
  projectionPathToNetWorth,
} from './road-chart'

describe('projectionPathToNetWorth', () => {
  it('adds non_invested to every point, since paths are invested balances only', () => {
    const path = [
      { month: '2027-01-01', balance: 400000 },
      { month: '2027-02-01', balance: 405000 },
    ]
    expect(projectionPathToNetWorth(path, 100000)).toEqual([
      { date: '2027-01-01', value: 500000 },
      { date: '2027-02-01', value: 505000 },
    ])
  })

  it('handles zero non_invested', () => {
    const path = [{ month: '2027-01-01', balance: 400000 }]
    expect(projectionPathToNetWorth(path, 0)).toEqual([{ date: '2027-01-01', value: 400000 }])
  })
})

describe('historyToRangePoints', () => {
  const point = (date: string, total: number) => ({ date, total, liquid: 0, buckets: {} as never, flags: [] })

  it('maps each point\'s total (already net worth) to a chart point', () => {
    const points = [point('2026-09-30', 490000), point('2026-10-14', 500000)]
    expect(historyToRangePoints(points, null)).toEqual([
      { date: '2026-09-30', value: 490000 },
      { date: '2026-10-14', value: 500000 },
    ])
  })

  it('drops points before `from` — back-computed estimates from before the accounts existed', () => {
    const points = [
      point('2025-10-01', 0),
      point('2026-07-01', 1000),
      point('2026-09-30', 490000),
      point('2026-10-14', 500000),
    ]
    expect(historyToRangePoints(points, '2026-09-13')).toEqual([
      { date: '2026-09-30', value: 490000 },
      { date: '2026-10-14', value: 500000 },
    ])
  })

  it('keeps a point dated exactly on `from`', () => {
    const points = [point('2026-09-13', 480000), point('2026-09-30', 490000)]
    expect(historyToRangePoints(points, '2026-09-13')).toEqual([
      { date: '2026-09-13', value: 480000 },
      { date: '2026-09-30', value: 490000 },
    ])
  })

  it('keeps every point when `from` is null', () => {
    const points = [point('2025-01-01', 0), point('2026-10-14', 500000)]
    expect(historyToRangePoints(points, null)).toEqual([
      { date: '2025-01-01', value: 0 },
      { date: '2026-10-14', value: 500000 },
    ])
  })
})

describe('buildProjectionTableRows', () => {
  it('merges low/base/high by date into one row per month', () => {
    const low = [{ date: '2027-01-01', value: 100 }]
    const base = [{ date: '2027-01-01', value: 150 }]
    const high = [{ date: '2027-01-01', value: 200 }]
    expect(buildProjectionTableRows(low, base, high)).toEqual([
      { id: '2027-01-01', month: '2027-01-01', low: 100, base: 150, high: 200 },
    ])
  })

  it('sorts rows by date and fills missing series with null', () => {
    const low = [{ date: '2027-02-01', value: 110 }]
    const base = [
      { date: '2027-01-01', value: 150 },
      { date: '2027-02-01', value: 160 },
    ]
    const high: { date: string; value: number }[] = []
    const rows = buildProjectionTableRows(low, base, high)
    expect(rows.map((r) => r.month)).toEqual(['2027-01-01', '2027-02-01'])
    expect(rows[0]).toEqual({ id: '2027-01-01', month: '2027-01-01', low: null, base: 150, high: null })
  })
})
