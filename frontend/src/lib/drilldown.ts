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
