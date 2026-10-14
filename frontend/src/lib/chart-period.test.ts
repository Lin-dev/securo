import { describe, expect, it } from 'vitest'

import { monthEnd, monthRange, monthStart, monthWindow } from '@/lib/chart-period'

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
