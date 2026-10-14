import { describe, expect, it } from 'vitest'

import { t } from '@/test/utils'
import type { WealthLeverResult, WealthLevers } from '@/types/wealth'

import { describeLever, formatSignedMonths, sortLevers } from './lever-helpers'

const money = (value: number) => `$${value.toLocaleString()}`
const maskedMoney = (value: number) => `MASKED($${value.toLocaleString()})`

const idleCashLever: WealthLeverResult = {
  key: 'idle_cash',
  amount: 20000,
  months_to_target: 80,
  date: '2033-06-01',
  label: 'Q2 2033',
  shift_months: -4,
}
const plus500Lever: WealthLeverResult = {
  key: 'plus_500',
  amount: 500,
  months_to_target: 82,
  date: '2033-08-01',
  label: 'Q3 2033',
  shift_months: -2,
}
const returnMinusLever: WealthLeverResult = {
  key: 'return_minus_1',
  amount: null,
  months_to_target: 90,
  date: '2034-04-01',
  label: 'Q2 2034',
  shift_months: 6,
}

const levers: WealthLevers = {
  currency: 'USD',
  as_of: '2026-10-14',
  cash: 62000,
  buffer: 36000,
  idle_cash: 26000,
  cash_cushion_months: 5.2,
  cash_flow: { typical_monthly: 1500, average_monthly: -400, months: 2 },
  top_controllable: { category: 'Dining out', monthly: 1200, band_cut: 600 },
  move: { rule: 'idle_cash', text: 'Move 26,000.00 USD of idle cash into your brokerage.', amount: 26000 },
  investing_exceeds_saving: false,
}

describe('sortLevers', () => {
  it('sorts by most months saved first, pushing a no-shift/unreachable lever last', () => {
    const sorted = sortLevers([returnMinusLever, plus500Lever, idleCashLever])
    expect(sorted.map((l) => l.key)).toEqual(['idle_cash', 'plus_500', 'return_minus_1'])
  })

  it('pushes a lever with no known date to the end, regardless of shift_months', () => {
    const unreachable: WealthLeverResult = { ...idleCashLever, key: 'plus_1000', date: null, months_to_target: null }
    const sorted = sortLevers([unreachable, plus500Lever])
    expect(sorted.map((l) => l.key)).toEqual(['plus_500', 'plus_1000'])
  })
})

describe('formatSignedMonths', () => {
  it('formats a negative shift with a minus sign', () => {
    expect(formatSignedMonths(-4)).toBe('−4')
  })

  it('formats a positive shift with a plus sign', () => {
    expect(formatSignedMonths(6)).toBe('+6')
  })
})

describe('describeLever', () => {
  it('prefers the current idle-cash figure (levers endpoint) over the projection lever amount', () => {
    const copy = describeLever(idleCashLever, levers, t, money, maskedMoney)
    expect(copy?.whatItIs).toBe(t('wealthRoad.levers.idleCash', { amount: 'MASKED($26,000)' }))
  })

  it('falls back to the lever\'s own amount when the levers query has not resolved', () => {
    const copy = describeLever(idleCashLever, undefined, t, money, maskedMoney)
    expect(copy?.whatItIs).toBe(t('wealthRoad.levers.idleCash', { amount: 'MASKED($20,000)' }))
  })

  it('names the top controllable category and its band cut for cut_top', () => {
    const cutTopLever: WealthLeverResult = { ...idleCashLever, key: 'cut_top', amount: null }
    const copy = describeLever(cutTopLever, levers, t, money, maskedMoney)
    expect(copy?.whatItIs).toBe(t('wealthRoad.levers.cutTop', { category: 'Dining out', amount: 'MASKED($600)' }))
  })

  it('skips cut_top entirely when there is no top controllable category', () => {
    const cutTopLever: WealthLeverResult = { ...idleCashLever, key: 'cut_top', amount: null }
    const noTopControllable: WealthLevers = { ...levers, top_controllable: null }
    expect(describeLever(cutTopLever, noTopControllable, t, money, maskedMoney)).toBeNull()
  })

  it('describes a return lever with fixed text and no amount', () => {
    const copy = describeLever(returnMinusLever, levers, t, money, maskedMoney)
    expect(copy?.whatItIs).toBe(t('wealthRoad.levers.returnLower'))
  })

  it('formats the date-shift line with the sign and the label', () => {
    const copy = describeLever(idleCashLever, levers, t, money, maskedMoney)
    expect(copy?.shift).toBe(t('wealthRoad.levers.shift', { months: '−4', label: 'Q2 2033' }))
  })

  it('describes an unreachable lever as not reachable', () => {
    const unreachable: WealthLeverResult = { ...idleCashLever, date: null, shift_months: null, label: null }
    const copy = describeLever(unreachable, levers, t, money, maskedMoney)
    expect(copy?.shift).toBe(t('wealthRoad.levers.notReachable'))
  })

  it('uses the PLAIN formatter for the fixed +$500/+$1,000/+$2,000 increments, never the masked one', () => {
    const plus1000: WealthLeverResult = { ...plus500Lever, key: 'plus_1000', amount: 1000 }
    const plus2000: WealthLeverResult = { ...plus500Lever, key: 'plus_2000', amount: 2000 }

    expect(describeLever(plus500Lever, levers, t, money, maskedMoney)?.whatItIs).toBe(
      t('wealthRoad.levers.plusMonthly', { amount: '$500' }),
    )
    expect(describeLever(plus1000, levers, t, money, maskedMoney)?.whatItIs).toBe(
      t('wealthRoad.levers.plusMonthly', { amount: '$1,000' }),
    )
    expect(describeLever(plus2000, levers, t, money, maskedMoney)?.whatItIs).toBe(
      t('wealthRoad.levers.plusMonthly', { amount: '$2,000' }),
    )
  })

  it('uses the MASKED formatter for idle_cash and cut_top, which come from the user\'s own data', () => {
    const cutTopLever: WealthLeverResult = { ...idleCashLever, key: 'cut_top', amount: null }

    expect(describeLever(idleCashLever, levers, t, money, maskedMoney)?.whatItIs).toContain('MASKED(')
    expect(describeLever(cutTopLever, levers, t, money, maskedMoney)?.whatItIs).toContain('MASKED(')
  })
})
