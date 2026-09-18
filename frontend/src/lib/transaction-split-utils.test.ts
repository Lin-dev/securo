import { describe, expect, it } from 'vitest'

import type { Transaction } from '@/types'

import {
  canSplitByCategory,
  isSplitChild,
  isSplitParent,
  linesFromChildren,
  newEditorLine,
  splitEvenly,
  splitRemainder,
  stripForSplitRole,
  toSplitLinesPayload,
  validateCategorySplitLines,
} from './transaction-split-utils'

function tx(overrides: Partial<Transaction> = {}): Transaction {
  return {
    id: 't1', user_id: 'u1', account_id: 'a1', category_id: null, category: null,
    external_id: null, description: 'NORTHWESTERN MUTUAL', original_description: null,
    amount: 500, currency: 'USD', date: '2026-09-03', type: 'debit', source: 'sync',
    status: 'posted', payee: null, payee_id: null, payee_name: null, notes: null,
    transfer_pair_id: null, amount_primary: null, fx_rate_used: null, fx_fallback: false,
    installment_number: null, total_installments: null, installment_total_amount: null,
    installment_purchase_date: null, installment_series_id: null, bill_id: null,
    effective_bill_date: null, splits: [], is_ignored: false,
    ...overrides,
  }
}

function line(category_id: string, amount: string, notes = '') {
  return newEditorLine({ category_id, amount, notes })
}

describe('split roles', () => {
  it('tells parents and lines apart by the fields the API sets', () => {
    expect(isSplitParent(tx({ split_count: 2 }))).toBe(true)
    expect(isSplitParent(tx())).toBe(false)
    expect(isSplitChild(tx({ parent_transaction_id: 'p' }))).toBe(true)
    expect(isSplitChild(tx())).toBe(false)
  })

  it('offers the editor only where the backend would accept a split', () => {
    expect(canSplitByCategory(tx())).toBe(true)
    expect(canSplitByCategory(tx({ split_count: 2, is_ignored: true }))).toBe(true)
    expect(canSplitByCategory(null)).toBe(false)
    expect(canSplitByCategory(tx({ status: 'pending' }))).toBe(false)
    expect(canSplitByCategory(tx({ parent_transaction_id: 'p' }))).toBe(false)
    expect(canSplitByCategory(tx({ transfer_pair_id: 'pair' }))).toBe(false)
    expect(canSplitByCategory(tx({ installment_number: 1 }))).toBe(false)
    expect(canSplitByCategory(tx({ source: 'opening_balance' }))).toBe(false)
    expect(canSplitByCategory(tx({ is_shared: true }))).toBe(false)
    expect(canSplitByCategory(tx({ virtual: true }))).toBe(false)
    expect(canSplitByCategory(tx({ splits: [{ id: 's', transaction_id: 't1', group_member_id: 'm', share_amount: 1, share_type: 'equal', share_pct: null, notes: null, created_at: '' }] }))).toBe(false)
  })
})

describe('line math', () => {
  it('splits evenly with the residue on the last line', () => {
    expect(splitEvenly(100, 3)).toEqual([33.33, 33.33, 33.34])
    expect(splitEvenly(-500, 2)).toEqual([250, 250])
    expect(splitEvenly(10, 0)).toEqual([])
  })

  it('reports the remainder in the display locale', () => {
    expect(splitRemainder(500, [line('a', '250'), line('b', '200')], 'en-US')).toBe(50)
    expect(splitRemainder(500, [line('a', '250,50'), line('b', '249,50')], 'de-DE')).toBe(0)
    expect(splitRemainder(500, [line('a', '300'), line('b', '300')], 'en-US')).toBe(-100)
  })

  it('validates two categorized positive lines that add up', () => {
    expect(validateCategorySplitLines(500, [line('a', '250'), line('b', '250')], 'en-US')).toEqual({ ok: true, errors: [] })
    expect(validateCategorySplitLines(500, [line('a', '500')], 'en-US').errors).toContain('needTwoLines')
    expect(validateCategorySplitLines(500, [line('', '250'), line('b', '250')], 'en-US').errors).toEqual(['needCategory'])
    expect(validateCategorySplitLines(500, [line('a', '0'), line('b', '500')], 'en-US').errors).toEqual(['needAmount'])
    expect(validateCategorySplitLines(500, [line('a', 'abc'), line('b', '500')], 'en-US').errors).toEqual(['needAmount'])
    expect(validateCategorySplitLines(500, [line('a', '250'), line('b', '200')], 'en-US').errors).toEqual(['unbalanced'])
    // Within half a cent is balanced.
    expect(validateCategorySplitLines(0.3, [line('a', '0.1'), line('b', '0.2')], 'en-US').ok).toBe(true)
  })
})

describe('payload conversion', () => {
  it('sends absolute cents, a null category for none and notes only when present', () => {
    expect(toSplitLinesPayload([line('a', '250', ' VUL '), line('', '250')], 'en-US')).toEqual([
      { category_id: 'a', amount: 250, notes: 'VUL' },
      { category_id: null, amount: 250 },
    ])
  })

  it('rebuilds editor lines from a parent\'s lines', () => {
    const lines = linesFromChildren(
      [tx({ id: 'c1', category_id: 'a', amount: 250, notes: 'x' }), tx({ id: 'c2', category_id: null, amount: -250.5 })],
      'de-DE',
    )
    expect(lines.map((l) => [l.category_id, l.amount, l.notes])).toEqual([['a', '250', 'x'], ['', '250,5', '']])
  })
})

describe('stripForSplitRole', () => {
  const payload = {
    description: 'd', amount: 1, date: '2026-01-01', type: 'debit', currency: 'USD',
    category_id: 'c', payee_id: null, account_id: 'a', notes: 'n', is_ignored: false,
    status: 'posted', exclude_from_pnl: true, splits: null,
  }

  it('leaves a line with its own bookkeeping only', () => {
    expect(stripForSplitRole(payload, { isSplitChild: true, isSplitParent: false })).toEqual({
      description: 'd', category_id: 'c', payee_id: null, notes: 'n',
    })
  })

  it('drops what a parent may not change and keeps what it passes on', () => {
    expect(stripForSplitRole(payload, { isSplitChild: false, isSplitParent: true })).toEqual({
      description: 'd', date: '2026-01-01', category_id: 'c', payee_id: null, notes: 'n', exclude_from_pnl: true,
    })
  })

  it('is a no-op for ordinary rows', () => {
    expect(stripForSplitRole(payload, { isSplitChild: false, isSplitParent: false })).toBe(payload)
  })
})
