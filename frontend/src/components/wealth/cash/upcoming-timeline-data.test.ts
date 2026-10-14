import { describe, expect, it } from 'vitest'

import type { WealthUpcomingCharge } from '@/types/wealth'

import { groupUpcomingByDate, isOnCardAccount } from './upcoming-timeline-data'

function charge(overrides: Partial<WealthUpcomingCharge> = {}): WealthUpcomingCharge {
  return {
    date: '2026-10-20',
    description: 'Streaming service',
    amount: 15.49,
    kind: 'recurring',
    account: 'Card',
    category: 'Subscriptions',
    recurring_id: 'rec-1',
    ...overrides,
  }
}

describe('groupUpcomingByDate', () => {
  it('groups out-of-order items into chronologically sorted date groups', () => {
    const items = [
      charge({ date: '2026-10-25', description: 'Card bill' }),
      charge({ date: '2026-10-20', description: 'Streaming' }),
      charge({ date: '2026-10-20', description: 'Gym' }),
    ]

    const groups = groupUpcomingByDate(items)

    expect(groups.map((g) => g.date)).toEqual(['2026-10-20', '2026-10-25'])
    expect(groups[0].items.map((i) => i.description)).toEqual(['Streaming', 'Gym'])
    expect(groups[1].items.map((i) => i.description)).toEqual(['Card bill'])
  })

  it('returns an empty array for no items', () => {
    expect(groupUpcomingByDate([])).toEqual([])
  })
})

describe('isOnCardAccount', () => {
  it('is true for a recurring charge on an account named with "card"', () => {
    expect(isOnCardAccount(charge({ kind: 'recurring', account: 'Card' }))).toBe(true)
  })

  it('is false for a recurring charge on a non-card account', () => {
    expect(isOnCardAccount(charge({ kind: 'recurring', account: 'Checking' }))).toBe(false)
  })

  it('is false for a card bill, even on a card account', () => {
    expect(isOnCardAccount(charge({ kind: 'card_bill', account: 'Card' }))).toBe(false)
  })

  it('is false when the account is null', () => {
    expect(isOnCardAccount(charge({ kind: 'recurring', account: null }))).toBe(false)
  })
})
