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

/** Sort largest-first, then fold everything past the cap into a single "Other". */
export function collapse(items: ReportCompositionItem[], otherLabel: string): ReportCompositionItem[] {
  const sorted = [...items].sort((a, b) => b.value - a.value)
  if (sorted.length <= MAX_NODES_PER_SIDE) return sorted
  const top = sorted.slice(0, MAX_NODES_PER_SIDE - 1)
  const rest = sorted.slice(MAX_NODES_PER_SIDE - 1)
  const otherValue = rest.reduce((s, c) => s + c.value, 0)
  return [
    ...top,
    { key: 'other', label: otherLabel, value: otherValue, color: NEUTRAL_COLOR, group: rest[0].group },
  ]
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

  // Left column, top → bottom: earned (green), contributed straight into
  // investments (blue), moved in (violet), and the deficit if the hub needs it.
  income.forEach((c, i) =>
    pushNode({ id: `in-${c.key}-${i}`, name: labelFor(c), color: colorFor(c, INCOME_COLOR), side: 'income' }),
  )
  if (contributions > 0) {
    pushNode({ id: 'contributions', name: labels.directContributions, color: INVEST_COLOR, side: 'contribution' })
  }
  transfersIn.forEach((c, i) =>
    pushNode({ id: `tin-${c.key}-${i}`, name: labelFor(c), color: colorFor(c, TRANSFER_COLOR), side: 'transfer_in' }),
  )
  // Deficit appears as an inflow on the income side so the centre balances.
  if (totals.net < 0) {
    pushNode({ id: 'deficit', name: labels.deficit, color: DEFICIT_COLOR, side: 'income' })
  }

  pushNode({ id: 'center', name: labels.center, color: CENTER_COLOR, side: 'center' })

  // Right column, top → bottom: expenses (red), investments (blue), transfers
  // out (violet), surplus (green) — matching the centre bar's stacked split.
  expense.forEach((c, i) =>
    pushNode({ id: `ex-${c.key}-${i}`, name: labelFor(c), color: colorFor(c, EXPENSE_COLOR), side: 'expense' }),
  )
  investment.forEach((c, i) =>
    pushNode({ id: `inv-${c.key}-${i}`, name: labelFor(c), color: colorFor(c, INVEST_COLOR), side: 'investment' }),
  )
  transfersOut.forEach((c, i) =>
    pushNode({ id: `tout-${c.key}-${i}`, name: labelFor(c), color: colorFor(c, TRANSFER_COLOR), side: 'transfer_out' }),
  )
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
