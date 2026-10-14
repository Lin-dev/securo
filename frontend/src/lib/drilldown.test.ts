import { describe, expect, it } from 'vitest'

import { resolveChartClick, transactionsUrl } from '@/lib/drilldown'

describe('transactionsUrl', () => {
  it('always includes from and to', () => {
    const url = transactionsUrl({ from: '2026-10-01', to: '2026-10-31' })
    expect(url).toBe('/transactions?from=2026-10-01&to=2026-10-31')
  })

  it('adds category_id and account_id when given', () => {
    const url = transactionsUrl({
      from: '2026-10-01',
      to: '2026-10-31',
      categoryId: 'cat-1',
      accountId: 'acct-2',
    })
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('category_id')).toBe('cat-1')
    expect(params.get('account_id')).toBe('acct-2')
  })

  it('omits category_id/account_id when not given', () => {
    const url = transactionsUrl({ from: '2026-10-01', to: '2026-10-31' })
    expect(url).not.toContain('category_id')
    expect(url).not.toContain('account_id')
  })

  it('forwards extra params only when truthy', () => {
    const url = transactionsUrl({
      from: '2026-10-01',
      to: '2026-10-31',
      extra: { q: 'coffee', payee_id: '' },
    })
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('q')).toBe('coffee')
    expect(params.has('payee_id')).toBe(false)
  })

  it('starts with /transactions', () => {
    expect(transactionsUrl({ from: '2026-01-01', to: '2026-01-31' }).startsWith('/transactions?')).toBe(true)
  })
})

const WINDOW = { from: '2026-10-01', to: '2026-10-31' }

describe('resolveChartClick', () => {
  it('opens the local drill-down for exactly one category id', () => {
    const result = resolveChartClick({
      categoryIds: ['cat-1'], accountIds: [], window: WINDOW, type: 'debit', wealthPnlOnly: true,
    })
    expect(result).toEqual({
      kind: 'single', categoryId: 'cat-1', accountId: undefined,
      type: 'debit', wealthPnlOnly: true, from: WINDOW.from, to: WINDOW.to,
    })
  })

  it('opens the local drill-down for exactly one account id', () => {
    const result = resolveChartClick({ categoryIds: [], accountIds: ['acct-1'], window: WINDOW })
    expect(result).toEqual({
      kind: 'single', categoryId: undefined, accountId: 'acct-1',
      type: undefined, wealthPnlOnly: undefined, from: WINDOW.from, to: WINDOW.to,
    })
  })

  it('links to Transactions with a comma-joined category_id list for 2+ category ids', () => {
    const result = resolveChartClick({
      categoryIds: ['cat-1', 'cat-2'], accountIds: [], window: WINDOW, wealthPnlOnly: true,
    })
    expect(result.kind).toBe('url')
    if (result.kind !== 'url') throw new Error('expected a url result')
    const params = new URLSearchParams(result.url.split('?')[1])
    expect(params.get('category_id')).toBe('cat-1,cat-2')
    expect(params.get('wealth_pnl_only')).toBe('1')
    expect(params.get('from')).toBe(WINDOW.from)
    expect(params.get('to')).toBe(WINDOW.to)
  })

  it('links to Transactions with a comma-joined account_id list for 2+ account ids', () => {
    const result = resolveChartClick({ categoryIds: [], accountIds: ['a1', 'a2'], window: WINDOW })
    expect(result.kind).toBe('url')
    if (result.kind !== 'url') throw new Error('expected a url result')
    const params = new URLSearchParams(result.url.split('?')[1])
    expect(params.get('account_id')).toBe('a1,a2')
  })

  it('falls back to a lane view when ids mix categories and accounts', () => {
    const result = resolveChartClick({
      categoryIds: ['cat-1'], accountIds: ['acct-1'], window: WINDOW, type: 'debit',
    })
    expect(result).toEqual({ kind: 'lane', exact: false, type: 'debit', wealthPnlOnly: undefined, from: WINDOW.from, to: WINDOW.to })
  })

  it('falls back to a lane view when there are no ids at all', () => {
    const result = resolveChartClick({ categoryIds: [], accountIds: [], window: WINDOW, type: 'credit', wealthPnlOnly: true })
    expect(result).toEqual({ kind: 'lane', exact: false, type: 'credit', wealthPnlOnly: true, from: WINDOW.from, to: WINDOW.to })
  })

  it('echoes the caller\'s own `exact` flag on a lane fallback (e.g. income/expenses under the P1 filter)', () => {
    const result = resolveChartClick({
      categoryIds: [], accountIds: [], window: WINDOW, type: 'credit', wealthPnlOnly: true, exact: true,
    })
    expect(result).toEqual({ kind: 'lane', exact: true, type: 'credit', wealthPnlOnly: true, from: WINDOW.from, to: WINDOW.to })
  })
})
