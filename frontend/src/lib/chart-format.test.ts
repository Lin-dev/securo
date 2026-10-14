import { describe, expect, it } from 'vitest'

import { formatMoney, formatMonth, formatPct, formatQuarter } from '@/lib/chart-format'

describe('formatMoney', () => {
  it('renders null/undefined as an em dash, like the canonical formatCurrency', () => {
    expect(formatMoney(null, 'USD')).toBe('—')
    expect(formatMoney(undefined, 'USD')).toBe('—')
  })

  it('formats a plain amount through the canonical currency formatter', () => {
    expect(formatMoney(1234.5, 'USD')).toBe('$1,234.50')
  })

  it('compacts large magnitudes for axis labels', () => {
    expect(formatMoney(1_234_000, 'USD', { compact: true })).toBe('$1.2M')
    expect(formatMoney(1_200, 'USD', { compact: true })).toBe('$1.2K')
  })

  it('prefixes a "+" for non-negative values when signed', () => {
    expect(formatMoney(500, 'USD', { signed: true })).toBe('+$500.00')
    expect(formatMoney(0, 'USD', { signed: true })).toBe('+$0.00')
    expect(formatMoney(-500, 'USD', { signed: true })).toBe('-$500.00')
  })

  it('never shows a bare negative zero, compact or not', () => {
    expect(formatMoney(-0.004, 'USD', { compact: true })).toBe('$0')
    expect(formatMoney(-0, 'USD')).not.toMatch(/^-/)
  })

  it('falls back to USD/en-US on a malformed currency code', () => {
    expect(formatMoney(10, 'NOT_A_CURRENCY')).toBe('$10.00')
  })
})

describe('formatPct', () => {
  it('formats a 0-1 fraction as a percentage with the default 1 digit', () => {
    expect(formatPct(0.164)).toBe('16.4%')
  })

  it('respects a custom digit count', () => {
    expect(formatPct(0.16449, 2)).toBe('16.45%')
    expect(formatPct(0.5, 0)).toBe('50%')
  })

  it('handles negative fractions', () => {
    expect(formatPct(-0.05)).toBe('-5.0%')
  })

  it('renders null/undefined/NaN as an em dash', () => {
    expect(formatPct(null)).toBe('—')
    expect(formatPct(undefined)).toBe('—')
    expect(formatPct(NaN)).toBe('—')
  })
})

describe('formatMonth', () => {
  it('formats an ISO date as a short month and year', () => {
    expect(formatMonth('2026-10-01')).toBe('Oct 2026')
  })

  it('ignores the day component', () => {
    expect(formatMonth('2026-10-31')).toBe('Oct 2026')
  })

  it('returns the input unchanged when it cannot be parsed', () => {
    expect(formatMonth('not-a-date')).toBe('not-a-date')
  })
})

describe('formatQuarter', () => {
  it.each([
    ['2026-01-15', 'Q1 2026'],
    ['2026-03-31', 'Q1 2026'],
    ['2026-04-01', 'Q2 2026'],
    ['2026-07-01', 'Q3 2026'],
    ['2026-10-01', 'Q4 2026'],
    ['2026-12-31', 'Q4 2026'],
  ])('maps %s to %s', (iso, expected) => {
    expect(formatQuarter(iso)).toBe(expected)
  })

  it('returns the input unchanged when it cannot be parsed', () => {
    expect(formatQuarter('garbage')).toBe('garbage')
  })
})
