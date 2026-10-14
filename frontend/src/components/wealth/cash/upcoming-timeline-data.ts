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
 * A card purchase reaches cash only through the card's bill (plan B5b). The
 * server says so on every item (`hits_cash`), so the "on card" marker reads
 * that flag instead of guessing from the account's name. A card bill always
 * hits cash, so it is never "on card".
 */
export function isOnCardAccount(item: Pick<WealthUpcomingCharge, 'kind' | 'hits_cash'>): boolean {
  return item.kind !== 'card_bill' && !item.hits_cash
}
