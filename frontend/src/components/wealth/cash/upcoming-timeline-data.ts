import type { WealthUpcomingCharge } from '@/types/wealth'

export interface UpcomingGroup {
  date: string
  items: WealthUpcomingCharge[]
}

/**
 * Groups upcoming items by date in chronological order. Items keep the
 * order the API returned them within a day.
 */
export function groupUpcomingByDate(items: WealthUpcomingCharge[]): UpcomingGroup[] {
  const order: string[] = []
  const byDate = new Map<string, WealthUpcomingCharge[]>()
  for (const item of items) {
    if (!byDate.has(item.date)) {
      byDate.set(item.date, [])
      order.push(item.date)
    }
    byDate.get(item.date)!.push(item)
  }
  return [...order].sort((a, b) => a.localeCompare(b)).map((date) => ({ date, items: byDate.get(date)! }))
}

/**
 * A card purchase reaches cash only through the card's bill (plan B5b), so
 * the server's `total` already excludes it -- this only decides the "on
 * card" marker shown next to the item. The contract gives no account-type
 * field on the charge itself, just its display name, so this stays a
 * display-only heuristic (never something the total depends on) and never
 * the only signal: the kind badge and the account name are shown regardless.
 */
export function isOnCardAccount(item: Pick<WealthUpcomingCharge, 'kind' | 'account'>): boolean {
  return item.kind === 'recurring' && !!item.account && /card/i.test(item.account)
}
