import type { ReportCompositionItem } from '@/types'

// Colour carries MEANING, not category identity: green = money in, red = money
// out, blue = money set aside, violet = money moved to or from accounts Securo
// cannot see, gray = uncategorised/folded. Category identity is carried by the
// always-on labels instead. Links fade from their source colour to their target
// colour, so an expense flow visibly turns from green (cash flow) to red at
// the category.
export const INCOME_COLOR = '#10B981' // emerald — money in
export const EXPENSE_COLOR = '#F43F5E' // rose — money out
export const INVEST_COLOR = '#0EA5E9' // sky blue — money set aside (investments)
export const TRANSFER_COLOR = '#8B5CF6' // violet — moved across the edge of what Securo sees
export const CENTER_COLOR = '#059669' // deeper emerald — the cash-flow hub
export const SURPLUS_COLOR = '#10B981' // emerald — leftover income
export const DEFICIT_COLOR = '#F59E0B' // amber — overspend drawn from reserves
export const NEUTRAL_COLOR = '#9CA3AF' // gray — uncategorised / folded long tail

// Keep the diagram legible: personal-finance Sankeys read best at ~a dozen
// streams per side. Beyond this we fold the smallest categories into "Other".
export const MAX_NODES_PER_SIDE = 9

// Left-to-right: the hub takes every inflow and hands it to every outflow.
// `contribution` is money that reached an investment account without passing
// through cash (a payroll 401(k) deduction): it enters on the left and leaves
// on the right for the same amount, so it never touches surplus.
export type MoneyMapSide =
  | 'income'
  | 'contribution'
  | 'transfer_in'
  | 'center'
  | 'expense'
  | 'investment'
  | 'transfer_out'

export const LEFT_SIDES: ReadonlySet<MoneyMapSide> = new Set(['income', 'contribution', 'transfer_in'])
export const RIGHT_SIDES: ReadonlySet<MoneyMapSide> = new Set(['expense', 'investment', 'transfer_out'])

export interface MoneyMapNode {
  id: string
  name: string
  color: string
  side: MoneyMapSide
  /** The category behind this node, when the composition item it was built
   * from carries a real category id (see `categoryIdFromKey`). Unset for
   * synthetic nodes (`center`, `surplus`, `deficit`, `contributions`) and
   * for the few lane rows the backend keys by account instead
   * (`accountId` below) or by a non-category token (`uncategorized`). */
  categoryId?: string
  /** The account behind this node, for the rare lane row the backend keys
   * by account rather than category (`account:<id>`) -- a direct
   * contribution that reached an investment account without passing
   * through cash. */
  accountId?: string
  /** Set only on a collapsed "Other" node: the raw composition keys
   * (category ids, `account:<id>`, or `uncategorized`/`direct_contributions`)
   * folded into it, so a click can drill into all of them. */
  foldedIds?: string[]
}

export interface MoneyMapLink {
  source: number
  target: number
  value: number
}

export interface MoneyMapLabels {
  other: string
  uncategorized: string
  deficit: string
  surplus: string
  center: string
  directContributions: string
}

export interface MoneyMapTotals {
  income: number
  contributions: number
  transfersIn: number
  expenses: number
  investments: number
  transfersOut: number
  /** income + transfers in − expenses − investing from cash − transfers out. Contributions cancel out. */
  net: number
}

export interface MoneyMap {
  nodes: MoneyMapNode[]
  links: MoneyMapLink[]
  hasData: boolean
  totals: MoneyMapTotals
}

const round2 = (v: number) => Math.round(v * 100) / 100

/** A composition item, plus (only on a synthetic "Other" row) the ids of
 * the items `collapse()` folded into it. */
export interface CollapsedCompositionItem extends ReportCompositionItem {
  foldedIds?: string[]
}

// Composition keys that never identify a real category -- the backend uses
// them as literal lane tokens (`report_service.py`'s Money Map comp_map),
// not a `Category.id`. Kept in lockstep with that file.
const NON_CATEGORY_KEYS: ReadonlySet<string> = new Set(['uncategorized', 'other', 'direct_contributions'])
const ACCOUNT_KEY_PREFIX = 'account:'

/** The real category id behind a composition `key`, or `undefined` when the
 * key is a non-category lane token (`uncategorized`, `other`,
 * `direct_contributions`) or an account-keyed row (`account:<id>`). */
export function categoryIdFromKey(key: string): string | undefined {
  if (NON_CATEGORY_KEYS.has(key) || key.startsWith(ACCOUNT_KEY_PREFIX)) return undefined
  return key
}

/** The real account id behind an account-keyed composition `key`
 * (`account:<id>` -- the direct-contribution credit leg, see
 * `report_service.py`'s Money Map lanes), or `undefined` for every other key. */
export function accountIdFromKey(key: string): string | undefined {
  return key.startsWith(ACCOUNT_KEY_PREFIX) ? key.slice(ACCOUNT_KEY_PREFIX.length) : undefined
}

/** Sort largest-first, then fold everything past the cap into a single
 * "Other", remembering which keys were folded (`foldedIds`) so a click on
 * "Other" can still drill into (some of) what it hides. */
export function collapse(items: ReportCompositionItem[], otherLabel: string): CollapsedCompositionItem[] {
  const sorted = [...items].sort((a, b) => b.value - a.value)
  if (sorted.length <= MAX_NODES_PER_SIDE) return sorted
  const top = sorted.slice(0, MAX_NODES_PER_SIDE - 1)
  const rest = sorted.slice(MAX_NODES_PER_SIDE - 1)
  const otherValue = rest.reduce((s, c) => s + c.value, 0)
  return [
    ...top,
    {
      key: 'other',
      label: otherLabel,
      value: otherValue,
      color: NEUTRAL_COLOR,
      group: rest[0].group,
      foldedIds: rest.map((c) => c.key),
    },
  ]
}

/** Every real category/account id behind the composition rows in the given
 * groups (e.g. `['transfers_in', 'transfers_out']` for the whole transfers
 * lane) -- the UNCOLLAPSED set, so a lane-level drill-down isn't limited to
 * the handful of rows still visible after `collapse()`. */
export function laneIds(
  composition: ReportCompositionItem[],
  groups: readonly string[],
): { categoryIds: string[]; accountIds: string[] } {
  const categoryIds: string[] = []
  const accountIds: string[] = []
  for (const c of composition) {
    if (c.value <= 0 || !groups.includes(c.group)) continue
    const categoryId = categoryIdFromKey(c.key)
    const accountId = accountIdFromKey(c.key)
    if (categoryId) categoryIds.push(categoryId)
    if (accountId) accountIds.push(accountId)
  }
  return { categoryIds, accountIds }
}

const sum = (items: ReportCompositionItem[]) => items.reduce((s, c) => s + c.value, 0)

/**
 * Turn the income/expenses composition into Sankey nodes and links.
 *
 * Groups read: `income`, `direct_contributions`, `transfers_in` on the left;
 * `expenses`, `investments`, `transfers_out` on the right. Nodes are pushed in
 * the order the columns should stack (top → bottom), which d3-sankey keeps
 * because every right-hand flow leaves the single hub in node order.
 */
export function buildMoneyMap(composition: ReportCompositionItem[], labels: MoneyMapLabels): MoneyMap {
  const positive = (group: string) => composition.filter((c) => c.group === group && c.value > 0)
  const income = collapse(positive('income'), labels.other)
  const transfersIn = collapse(positive('transfers_in'), labels.other)
  const expense = collapse(positive('expenses'), labels.other)
  const investment = collapse(positive('investments'), labels.other)
  const transfersOut = collapse(positive('transfers_out'), labels.other)
  const contributions = sum(positive('direct_contributions'))

  const totals: MoneyMapTotals = {
    income: sum(income),
    contributions,
    transfersIn: sum(transfersIn),
    expenses: sum(expense),
    investments: sum(investment),
    transfersOut: sum(transfersOut),
    net: 0,
  }
  // Surplus is what's left after spending, investing and moving money out —
  // so investing shrinks surplus instead of silently inflating it. Direct
  // contributions sit in both `contributions` and `investments`, so they
  // cancel here, as they should: that money never passed through cash.
  totals.net = round2(
    totals.income + totals.contributions + totals.transfersIn
      - totals.expenses - totals.investments - totals.transfersOut,
  )

  const nodes: MoneyMapNode[] = []
  const links: MoneyMapLink[] = []
  if (
    income.length === 0 && transfersIn.length === 0 && contributions <= 0
    && expense.length === 0 && investment.length === 0 && transfersOut.length === 0
  ) {
    return { nodes, links, hasData: false, totals }
  }

  const indexOf = new Map<string, number>()
  const pushNode = (n: MoneyMapNode) => {
    indexOf.set(n.id, nodes.length)
    nodes.push(n)
    return nodes.length - 1
  }
  const labelFor = (c: ReportCompositionItem) =>
    c.key === 'uncategorized' ? labels.uncategorized
      : c.key === 'other' ? labels.other
        : c.label
  const isNeutral = (c: ReportCompositionItem) => c.key === 'uncategorized' || c.key === 'other'
  const colorFor = (c: ReportCompositionItem, color: string) => (isNeutral(c) ? NEUTRAL_COLOR : color)
  // Every category/account-composition node is built the same way: an id
  // namespaced by side and index, the translated/neutral label and colour,
  // and -- plan S5 -- the ids a click needs to drill back into Transactions.
  const pushCompositionNode = (
    prefix: string, c: CollapsedCompositionItem, i: number, side: MoneyMapSide, color: string,
  ) =>
    pushNode({
      id: `${prefix}-${c.key}-${i}`,
      name: labelFor(c),
      color: colorFor(c, color),
      side,
      categoryId: categoryIdFromKey(c.key),
      accountId: accountIdFromKey(c.key),
      foldedIds: c.foldedIds,
    })

  // Left column, top → bottom: earned (green), contributed straight into
  // investments (blue), moved in (violet), and the deficit if the hub needs it.
  income.forEach((c, i) => pushCompositionNode('in', c, i, 'income', INCOME_COLOR))
  if (contributions > 0) {
    pushNode({ id: 'contributions', name: labels.directContributions, color: INVEST_COLOR, side: 'contribution' })
  }
  transfersIn.forEach((c, i) => pushCompositionNode('tin', c, i, 'transfer_in', TRANSFER_COLOR))
  // Deficit appears as an inflow on the income side so the centre balances.
  if (totals.net < 0) {
    pushNode({ id: 'deficit', name: labels.deficit, color: DEFICIT_COLOR, side: 'income' })
  }

  pushNode({ id: 'center', name: labels.center, color: CENTER_COLOR, side: 'center' })

  // Right column, top → bottom: expenses (red), investments (blue), transfers
  // out (violet), surplus (green) — matching the centre bar's stacked split.
  expense.forEach((c, i) => pushCompositionNode('ex', c, i, 'expense', EXPENSE_COLOR))
  investment.forEach((c, i) => pushCompositionNode('inv', c, i, 'investment', INVEST_COLOR))
  transfersOut.forEach((c, i) => pushCompositionNode('tout', c, i, 'transfer_out', TRANSFER_COLOR))
  if (totals.net > 0) {
    pushNode({ id: 'surplus', name: labels.surplus, color: SURPLUS_COLOR, side: 'expense' })
  }

  const link = (from: string, to: string, value: number) =>
    links.push({ source: indexOf.get(from)!, target: indexOf.get(to)!, value })

  income.forEach((c, i) => link(`in-${c.key}-${i}`, 'center', c.value))
  if (contributions > 0) link('contributions', 'center', contributions)
  transfersIn.forEach((c, i) => link(`tin-${c.key}-${i}`, 'center', c.value))
  if (totals.net < 0) link('deficit', 'center', -totals.net)
  expense.forEach((c, i) => link('center', `ex-${c.key}-${i}`, c.value))
  investment.forEach((c, i) => link('center', `inv-${c.key}-${i}`, c.value))
  transfersOut.forEach((c, i) => link('center', `tout-${c.key}-${i}`, c.value))
  if (totals.net > 0) link('center', 'surplus', totals.net)

  return { nodes, links, hasData: true, totals }
}
