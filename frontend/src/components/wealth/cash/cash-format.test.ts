import { describe, expect, it } from 'vitest'

import { formatCashDay } from './cash-format'

describe('formatCashDay', () => {
  it('formats an ISO date as a short month and day', () => {
    expect(formatCashDay('2026-10-20', 'en-US')).toBe('Oct 20')
  })

  it('falls back to en-US for a malformed locale tag', () => {
    expect(formatCashDay('2026-11-12', '')).toBe('Nov 12')
  })

  it('returns the original string for an unparsable date', () => {
    expect(formatCashDay('not-a-date', 'en-US')).toBe('not-a-date')
  })
})
