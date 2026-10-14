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
    hits_cash: false,
    estimated: false,
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
  it('is true for a charge that does not hit cash (a card purchase), whatever the account is called', () => {
    expect(isOnCardAccount(charge({ kind: 'recurring', account: 'nathaniel', hits_cash: false }))).toBe(true)
    expect(isOnCardAccount(charge({ kind: 'pending', account: 'Amazon Prime Rewards Visa Signature', hits_cash: false }))).toBe(true)
  })

  it('is false for a charge that hits cash', () => {
    expect(isOnCardAccount(charge({ kind: 'recurring', account: 'Checking', hits_cash: true }))).toBe(false)
  })

  it('is false for a card bill', () => {
    expect(isOnCardAccount(charge({ kind: 'card_bill', account: 'Card', hits_cash: true }))).toBe(false)
  })
})
