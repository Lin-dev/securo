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
  it('maps each point\'s total (already net worth) to a chart point', () => {
    const points = [
      { date: '2026-09-30', total: 490000, liquid: 195000, buckets: {} as never, flags: [] },
      { date: '2026-10-14', total: 500000, liquid: 200000, buckets: {} as never, flags: [] },
    ]
    expect(historyToRangePoints(points)).toEqual([
      { date: '2026-09-30', value: 490000 },
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
