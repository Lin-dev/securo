import { describe, expect, it } from 'vitest'

import { localDateString } from '@/lib/date-utils'

import { resolveBudgetPaceToday } from './budget-pace'

describe('resolveBudgetPaceToday', () => {
  // 23:30 UTC on May 31 is still May 31 in Sao Paulo (UTC-3) but already
  // June 1 in Tokyo (UTC+9) -- a fixed instant where the day AND the month
  // differ depending on whose clock you read.
  const instant = new Date('2026-05-31T23:30:00Z')

  it('resolves the workspace zone\'s own day and month, not the browser\'s', () => {
    expect(resolveBudgetPaceToday('Asia/Tokyo', instant)).toEqual({ month: '2026-06', dayOfMonth: 1 })
    expect(resolveBudgetPaceToday('America/Sao_Paulo', instant)).toEqual({ month: '2026-05', dayOfMonth: 31 })
  })

  it('falls back to the browser date when there is no workspace timezone', () => {
    const browserToday = localDateString(instant)
    expect(resolveBudgetPaceToday(undefined, instant)).toEqual({
      month: browserToday.slice(0, 7),
      dayOfMonth: Number(browserToday.slice(8, 10)),
    })
  })

  it('falls back to the browser date for an unrecognized timezone', () => {
    const browserToday = localDateString(instant)
    expect(resolveBudgetPaceToday('Mars/Olympus_Mons', instant)).toEqual({
      month: browserToday.slice(0, 7),
      dayOfMonth: Number(browserToday.slice(8, 10)),
    })
  })
})
