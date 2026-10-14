/**
 * Build a link from a chart into the Transactions page (P8: "Clicking a
 * chart opens the drill-down, which has a 'View in Transactions' link
 * carrying `from`/`to`.").
 *
 * `from`/`to` are NOT optional here. `pages/transactions.tsx` only seeds its
 * date range from the URL when at least one of `?from`/`?to` is present; with
 * neither, it defaults to the current calendar month (see its `filterFrom`/
 * `filterTo` initializers). A chart point from any other month would silently
 * land on a page showing the wrong month's transactions instead.
 *
 * Param names match what `transactions.tsx` actually reads from its
 * `useSearchParams()`: `from`, `to`, `category_id`, `account_id`.
 */
export interface TransactionsUrlParams {
  from: string
  to: string
  categoryId?: string
  accountId?: string
  /** Extra query params (e.g. `q`, `type`) forwarded as-is when truthy. */
  extra?: Record<string, string | undefined>
}

export function transactionsUrl({ from, to, categoryId, accountId, extra }: TransactionsUrlParams): string {
  const params = new URLSearchParams()
  params.set('from', from)
  params.set('to', to)
  if (categoryId) params.set('category_id', categoryId)
  if (accountId) params.set('account_id', accountId)
  if (extra) {
    for (const [key, value] of Object.entries(extra)) {
      if (value) params.set(key, value)
    }
  }
  return `/transactions?${params.toString()}`
}

/**
 * What a chart click resolved into: a category/account behind exactly one
 * value, several values Transactions can still filter exactly (a
 * comma-joined id list), or a value no exact filter represents, where the
 * caller drills into the coarsest honest view instead (plan S5: the Money
 * Map's "Other" and lane clicks).
 */
export type ChartClickResolution =
  | {
      kind: 'single'
      categoryId?: string
      accountId?: string
      type?: 'credit' | 'debit'
      wealthPnlOnly?: boolean
      from: string
      to: string
    }
  | { kind: 'url'; url: string }
  | {
      kind: 'lane'
      /** Whether `type` (plus `wealthPnlOnly`, for income/expenses) already
       * pins the lane down exactly -- e.g. every income row IS
       * `type: 'credit'` under the P1 filter -- rather than this being a
       * last-resort view standing in for ids that didn't survive, or that
       * mixed categories and accounts (which Transactions' all-AND filters
       * can't OR together). Echoes the caller's own `exact`; a caller that
       * omits it gets `false`, since only the caller knows its domain. */
      exact: boolean
      type?: 'credit' | 'debit'
      wealthPnlOnly?: boolean
      from: string
      to: string
    }

export interface ChartClickTarget {
  categoryIds: string[]
  accountIds: string[]
  window: { from: string; to: string }
  type?: 'credit' | 'debit'
  wealthPnlOnly?: boolean
  /** Set by the caller when it KNOWS `type`/`wealthPnlOnly` alone exactly
   * describe the lane (e.g. the income/expenses lanes under the P1 filter),
   * so a `{ kind: 'lane' }` result isn't mistaken for the ids-didn't-survive
   * fallback. Only consulted when there are no ids to resolve. */
  exact?: boolean
}

/**
 * Turn a chart click -- the category/account ids behind it, the report
 * window, and (optionally) the row `type` and the P1 `wealth_pnl_only`
 * filter -- into either a same-page drill-down or a Transactions URL.
 *
 * Transactions' own filters all AND together: there is no way to ask for
 * "any of these categories OR any of these accounts". So:
 *  - exactly one id (of either kind) opens the local drill-down scoped to it
 *  - two or more ids of the SAME kind become a Transactions URL with a
 *    comma-joined `category_id`/`account_id` list (the page already reads
 *    either as a list)
 *  - a mix of both kinds, or neither, can't be represented exactly -- the
 *    caller gets `{ kind: 'lane' }` back (`exact: false` unless `type` alone
 *    already nails it down) and should drill into the window, optionally
 *    narrowed by `type`, and make clear in the UI that the view is a
 *    best-effort stand-in for the lane rather than that exact set of rows.
 */
export function resolveChartClick(target: ChartClickTarget): ChartClickResolution {
  const { categoryIds, accountIds, window, type, wealthPnlOnly, exact } = target
  const { from, to } = window

  if (categoryIds.length > 0 && accountIds.length === 0) {
    if (categoryIds.length === 1) {
      return { kind: 'single', categoryId: categoryIds[0], type, wealthPnlOnly, from, to }
    }
    return {
      kind: 'url',
      url: transactionsUrl({
        from, to,
        categoryId: categoryIds.join(','),
        extra: wealthPnlOnly ? { wealth_pnl_only: '1' } : undefined,
      }),
    }
  }

  if (accountIds.length > 0 && categoryIds.length === 0) {
    if (accountIds.length === 1) {
      return { kind: 'single', accountId: accountIds[0], type, wealthPnlOnly, from, to }
    }
    return {
      kind: 'url',
      url: transactionsUrl({
        from, to,
        accountId: accountIds.join(','),
        extra: wealthPnlOnly ? { wealth_pnl_only: '1' } : undefined,
      }),
    }
  }

  return { kind: 'lane', exact: exact ?? false, type, wealthPnlOnly, from, to }
}
