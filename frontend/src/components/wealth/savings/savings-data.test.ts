import { describe, expect, it } from 'vitest'

import { domainMax, domainMin, isClipped, latestMonthRate, latestRollingRate, toChartRows, visibleMonths } from './savings-data'
import type { WealthSavingsMonth } from '@/types/wealth'

function month(over: Partial<WealthSavingsMonth>): WealthSavingsMonth {
  return {
    month: '2026-08-01',
    income: 15000,
    expenses: 11000,
    direct: 2000,
    rate: 0.353,
    cash_rate: 0.267,
    rolling_3m: null,
    rolling_12m: null,
    in_coverage: true,
    partial: false,
    ...over,
  }
}

const months: WealthSavingsMonth[] = [
  month({ month: '2026-08-01', rate: 0.353, cash_rate: 0.267 }),
  month({ month: '2026-09-01', rate: 0.294, cash_rate: 0.2 }),
  month({ month: '2026-10-01', rate: 0.25, cash_rate: 0.143, rolling_3m: 0.31, partial: true }),
]

describe('toChartRows', () => {
  it('uses the P1 rate when useCashRate is false', () => {
    const rows = toChartRows(months, false)
    expect(rows.map((r) => r.value)).toEqual([0.353, 0.294, 0.25])
  })

  it('uses cash_rate when useCashRate is true', () => {
    const rows = toChartRows(months, true)
    expect(rows.map((r) => r.value)).toEqual([0.267, 0.2, 0.143])
  })

  it('carries the rolling_3m, partial and in_coverage flags through unchanged', () => {
    const rows = toChartRows(months, false)
    expect(rows[2]).toEqual({ month: '2026-10-01', value: 0.25, rolling3m: 0.31, partial: true, inCoverage: true })
  })

  it('sorts rows by month', () => {
    const shuffled = [months[2], months[0], months[1]]
    const rows = toChartRows(shuffled, false)
    expect(rows.map((r) => r.month)).toEqual(['2026-08-01', '2026-09-01', '2026-10-01'])
  })

  it('marks a month outside coverage', () => {
    const withGap = [...months, month({ month: '2026-11-01', in_coverage: false, rate: null, cash_rate: null })]
    const rows = toChartRows(withGap, false)
    expect(rows[3]).toMatchObject({ inCoverage: false, value: null })
  })
})

describe('latestRollingRate', () => {
  it('finds the most recent month with a non-null rolling_3m', () => {
    expect(latestRollingRate(months)).toEqual({ value: 0.31, month: '2026-10-01' })
  })

  it('returns null when no month has a rolling_3m yet', () => {
    const noRolling = months.map((m) => ({ ...m, rolling_3m: null }))
    expect(latestRollingRate(noRolling)).toBeNull()
  })

  it('skips a trailing null to find an earlier rolling value', () => {
    const withTrailingGap = [
      month({ month: '2026-08-01', rolling_3m: 0.28 }),
      month({ month: '2026-09-01', rolling_3m: null }),
    ]
    expect(latestRollingRate(withTrailingGap)).toEqual({ value: 0.28, month: '2026-08-01' })
  })

  it('returns null for an empty list', () => {
    expect(latestRollingRate([])).toBeNull()
  })
})

describe('latestMonthRate', () => {
  it("returns the latest month's P1 rate", () => {
    expect(latestMonthRate(months, false)).toBe(0.25)
  })

  it("returns the latest month's cash rate", () => {
    expect(latestMonthRate(months, true)).toBe(0.143)
  })

  it('returns null for an empty list', () => {
    expect(latestMonthRate([], false)).toBeNull()
  })
})

describe('visibleMonths', () => {
  const withStrayRows = [
    month({ month: '2026-04-01', in_coverage: false, partial: false, rate: -3.4, cash_rate: -3.4 }),
    month({ month: '2026-05-01', in_coverage: false, partial: false, rate: -1.1, cash_rate: -1.1 }),
    ...months,
  ]

  it('defaults to in-coverage months plus the partial month, dropping stray pre-coverage rows', () => {
    const visible = visibleMonths(withStrayRows, false)
    expect(visible.map((m) => m.month)).toEqual(['2026-08-01', '2026-09-01', '2026-10-01'])
  })

  it('keeps every month once "show earlier months" is on', () => {
    const visible = visibleMonths(withStrayRows, true)
    expect(visible).toHaveLength(5)
  })

  it('keeps a partial month even when it is not yet in coverage', () => {
    const notYetCovered = [month({ month: '2026-10-01', in_coverage: false, partial: true })]
    expect(visibleMonths(notYetCovered, false)).toEqual(notYetCovered)
  })
})

describe('isClipped', () => {
  it('is false within [-100%, 100%]', () => {
    expect(isClipped(0.31)).toBe(false)
    expect(isClipped(-1)).toBe(false)
    expect(isClipped(1)).toBe(false)
  })

  it('is true beyond ±100%', () => {
    expect(isClipped(-2.34)).toBe(true)
    expect(isClipped(1.01)).toBe(true)
  })

  it('is false for null', () => {
    expect(isClipped(null)).toBe(false)
  })
})

describe('domainMin/domainMax', () => {
  it('includes 0 and tracks a narrow, unremarkable data range', () => {
    expect(domainMin(-0.05)).toBe(-0.05)
    expect(domainMax(0.3)).toBeCloseTo(0.605)
  })

  it('clamps to [-100%, 100%] however far an outlier runs', () => {
    expect(domainMin(-2.34)).toBe(-1)
    expect(domainMax(5)).toBe(1)
  })

  it('still includes 0 when every visible value is positive or negative', () => {
    expect(domainMin(0.1)).toBe(0)
    expect(domainMax(-0.1)).toBeCloseTo(0.605)
  })
})
