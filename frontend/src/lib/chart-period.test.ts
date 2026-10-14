import { describe, expect, it } from 'vitest'

import {
  monthEnd,
  monthRange,
  monthStart,
  monthWindow,
  periodWindow,
  previousWindow,
  weekWindow,
  yearWindow,
} from '@/lib/chart-period'

describe('monthStart', () => {
  it('returns the first day of the month for any day in it', () => {
    expect(monthStart('2026-10-14')).toBe('2026-10-01')
    expect(monthStart('2026-10-01')).toBe('2026-10-01')
    expect(monthStart('2026-10-31')).toBe('2026-10-01')
  })
})

describe('monthEnd', () => {
  it('handles 31-day months', () => {
    expect(monthEnd('2026-10-05')).toBe('2026-10-31')
  })

  it('handles 30-day months', () => {
    expect(monthEnd('2026-04-05')).toBe('2026-04-30')
  })

  it('handles February in a leap year', () => {
    expect(monthEnd('2028-02-01')).toBe('2028-02-29')
  })

  it('handles February in a non-leap year', () => {
    expect(monthEnd('2026-02-01')).toBe('2026-02-28')
  })
})

describe('monthWindow', () => {
  it('returns the {from, to} window for the month containing the date', () => {
    expect(monthWindow('2026-10-14')).toEqual({ from: '2026-10-01', to: '2026-10-31' })
  })
})

describe('monthRange', () => {
  it('returns a single month-start when from and to fall in the same month', () => {
    expect(monthRange('2026-10-01', '2026-10-31')).toEqual(['2026-10-01'])
  })

  it('returns one entry per month, inclusive of both endpoints', () => {
    expect(monthRange('2026-10-14', '2027-01-05')).toEqual([
      '2026-10-01',
      '2026-11-01',
      '2026-12-01',
      '2027-01-01',
    ])
  })

  it('crosses a year boundary correctly', () => {
    expect(monthRange('2026-12-01', '2027-02-01')).toEqual([
      '2026-12-01',
      '2027-01-01',
      '2027-02-01',
    ])
  })

  it('returns an empty array when the range is backwards', () => {
    expect(monthRange('2027-01-01', '2026-01-01')).toEqual([])
  })
})

describe('weekWindow', () => {
  it('returns Monday through Sunday for an ISO week label', () => {
    // 2021-01-04 is a Monday, and ISO week 1 always contains January 4.
    expect(weekWindow('2021-W01')).toEqual({ from: '2021-01-04', to: '2021-01-10' })
  })

  it('crosses a year boundary when week 1 starts in the prior December', () => {
    // 2026-01-01 is a Thursday, so ISO week 1 of 2026 starts the Monday before it.
    expect(weekWindow('2026-W01')).toEqual({ from: '2025-12-29', to: '2026-01-04' })
  })
})

describe('yearWindow', () => {
  it('returns January 1 through December 31 for a year label', () => {
    expect(yearWindow('2026')).toEqual({ from: '2026-01-01', to: '2026-12-31' })
  })
})

describe('periodWindow', () => {
  it('treats a daily label as its own single-day window', () => {
    expect(periodWindow('2026-10-14', 'daily')).toEqual({ from: '2026-10-14', to: '2026-10-14' })
  })

  it('delegates to weekWindow for a weekly label', () => {
    expect(periodWindow('2021-W01', 'weekly')).toEqual(weekWindow('2021-W01'))
  })

  it('delegates to monthWindow for a monthly label', () => {
    expect(periodWindow('2026-10', 'monthly')).toEqual(monthWindow('2026-10'))
  })

  it('delegates to yearWindow for a yearly label', () => {
    expect(periodWindow('2026', 'yearly')).toEqual(yearWindow('2026'))
  })

  it('falls back to treating an unknown interval as a single-day label', () => {
    expect(periodWindow('2026-10-14', 'unknown')).toEqual({ from: '2026-10-14', to: '2026-10-14' })
  })
})

describe('previousWindow', () => {
  it('shifts a quarter-length window back by its own length, ending the day before it starts', () => {
    expect(previousWindow('2026-07-01', '2026-09-30')).toEqual({ from: '2026-03-31', to: '2026-06-30' })
  })

  it('shifts a single-day window back by one day', () => {
    expect(previousWindow('2026-09-12', '2026-09-12')).toEqual({ from: '2026-09-11', to: '2026-09-11' })
  })

  it('crosses a year boundary', () => {
    expect(previousWindow('2026-01-01', '2026-01-31')).toEqual({ from: '2025-12-01', to: '2025-12-31' })
  })
})
