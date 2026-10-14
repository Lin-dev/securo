import { describe, expect, it } from 'vitest'

import { transactionsUrl } from '@/lib/drilldown'

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
