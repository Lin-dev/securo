import { describe, expect, it } from 'vitest'

import { resolveClickedDay } from './day-drilldown'

const data = [{ day: 1 }, { day: 2 }, { day: 3 }]

describe('resolveClickedDay', () => {
  it('resolves the day from a numeric activeTooltipIndex', () => {
    expect(resolveClickedDay(0, data)).toBe(1)
    expect(resolveClickedDay(2, data)).toBe(3)
  })

  it('resolves the day from a numeric-string activeTooltipIndex (recharts 3 can hand back either)', () => {
    expect(resolveClickedDay('1', data)).toBe(2)
  })

  it('returns null when there is no active index (click missed every point)', () => {
    expect(resolveClickedDay(undefined, data)).toBeNull()
    expect(resolveClickedDay(null, data)).toBeNull()
  })

  it('returns null for an out-of-range or non-numeric index rather than throwing', () => {
    expect(resolveClickedDay(-1, data)).toBeNull()
    expect(resolveClickedDay(99, data)).toBeNull()
    expect(resolveClickedDay('not-a-number', data)).toBeNull()
  })

  it('returns null against an empty data array', () => {
    expect(resolveClickedDay(0, [])).toBeNull()
  })
})
