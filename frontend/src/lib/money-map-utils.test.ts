import { describe, expect, it } from 'vitest'
import type { ReportCompositionItem } from '@/types'
import {
  buildMoneyMap,
  collapse,
  DEFICIT_COLOR,
  INVEST_COLOR,
  MAX_NODES_PER_SIDE,
  NEUTRAL_COLOR,
  TRANSFER_COLOR,
} from './money-map-utils'

const labels = {
  other: 'Other',
  uncategorized: 'Uncategorized',
  deficit: 'Deficit',
  surplus: 'Surplus',
  center: 'Cash Flow',
  directContributions: 'Direct contributions',
}

const item = (group: string, key: string, value: number, label = key): ReportCompositionItem => ({
  key, label, value, color: '#000000', group,
})

const nodeNamed = (map: ReturnType<typeof buildMoneyMap>, name: string) =>
  map.nodes.find((n) => n.name === name)

const valueInto = (map: ReturnType<typeof buildMoneyMap>, name: string) => {
  const idx = map.nodes.findIndex((n) => n.name === name)
  return map.links.filter((l) => l.target === idx).reduce((s, l) => s + l.value, 0)
}
const valueOutOf = (map: ReturnType<typeof buildMoneyMap>, name: string) => {
  const idx = map.nodes.findIndex((n) => n.name === name)
  return map.links.filter((l) => l.source === idx).reduce((s, l) => s + l.value, 0)
}

describe('buildMoneyMap', () => {
  it('has no data when every lane is empty', () => {
    const map = buildMoneyMap([item('income', 'x', 0)], labels)
    expect(map.hasData).toBe(false)
    expect(map.nodes).toEqual([])
  })

  it('draws surplus as what is left after spending, investing and moving money out', () => {
    const map = buildMoneyMap([
      item('income', 'salary', 1000, 'Salary'),
      item('expenses', 'rent', 400, 'Rent'),
      item('investments', 'inv', 250, 'Investment contribution'),
      item('transfers_out', 'card', 100, 'Card payment'),
      item('transfers_in', 'trust', 50, 'Held in trust'),
    ], labels)
    expect(map.totals.net).toBe(300)
    expect(valueInto(map, 'Surplus')).toBe(300)
    expect(nodeNamed(map, 'Deficit')).toBeUndefined()
    expect(nodeNamed(map, 'Card payment')?.side).toBe('transfer_out')
    expect(nodeNamed(map, 'Card payment')?.color).toBe(TRANSFER_COLOR)
    expect(nodeNamed(map, 'Held in trust')?.side).toBe('transfer_in')
    expect(valueInto(map, 'Cash Flow')).toBe(1050)
    expect(valueOutOf(map, 'Cash Flow')).toBe(1050)
  })

  it('shows direct contributions on both sides without touching surplus', () => {
    const base = [
      item('income', 'salary', 1000, 'Salary'),
      item('expenses', 'rent', 400, 'Rent'),
    ]
    const without = buildMoneyMap(base, labels)
    const withPlan = buildMoneyMap([
      ...base,
      item('direct_contributions', 'direct_contributions', 700, 'Direct contributions'),
      item('investments', 'account:plan', 700, 'Employer 401(k)'),
    ], labels)
    expect(withPlan.totals.net).toBe(without.totals.net)
    expect(valueInto(withPlan, 'Surplus')).toBe(600)
    const contrib = nodeNamed(withPlan, 'Direct contributions')
    expect(contrib?.side).toBe('contribution')
    expect(contrib?.color).toBe(INVEST_COLOR)
    expect(valueOutOf(withPlan, 'Direct contributions')).toBe(700)
    expect(nodeNamed(withPlan, 'Employer 401(k)')?.side).toBe('investment')
    expect(valueInto(withPlan, 'Employer 401(k)')).toBe(700)
  })

  it('adds a deficit inflow when outflows exceed inflows', () => {
    const map = buildMoneyMap([
      item('income', 'salary', 500, 'Salary'),
      item('expenses', 'rent', 800, 'Rent'),
    ], labels)
    expect(map.totals.net).toBe(-300)
    const deficit = nodeNamed(map, 'Deficit')
    expect(deficit?.color).toBe(DEFICIT_COLOR)
    expect(valueOutOf(map, 'Deficit')).toBe(300)
    expect(nodeNamed(map, 'Surplus')).toBeUndefined()
  })

  it('stacks the right column expenses, investments, transfers out, surplus', () => {
    const map = buildMoneyMap([
      item('income', 'salary', 1000, 'Salary'),
      item('transfers_out', 'card', 100, 'Card payment'),
      item('investments', 'inv', 200, 'Investing'),
      item('expenses', 'rent', 300, 'Rent'),
    ], labels)
    const center = map.nodes.findIndex((n) => n.id === 'center')
    const rightSides = map.nodes.slice(center + 1).map((n) => n.side)
    expect(rightSides).toEqual(['expense', 'investment', 'transfer_out', 'expense'])
    expect(map.nodes[map.nodes.length - 1].id).toBe('surplus')
  })

  it('translates the neutral buckets and paints them gray', () => {
    const map = buildMoneyMap([
      item('income', 'uncategorized', 10, 'whatever'),
      item('expenses', 'rent', 5, 'Rent'),
    ], labels)
    const unc = nodeNamed(map, 'Uncategorized')
    expect(unc?.color).toBe(NEUTRAL_COLOR)
  })
})

describe('collapse', () => {
  it('folds the long tail into Other and keeps the biggest first', () => {
    const items = Array.from({ length: 12 }, (_, i) => item('expenses', `c${i}`, i + 1))
    const out = collapse(items, 'Other')
    expect(out).toHaveLength(MAX_NODES_PER_SIDE)
    expect(out[0].value).toBe(12)
    const other = out[out.length - 1]
    expect(other.key).toBe('other')
    expect(other.label).toBe('Other')
    expect(other.value).toBe(1 + 2 + 3 + 4)
    expect(other.color).toBe(NEUTRAL_COLOR)
  })

  it('leaves short lists alone apart from sorting', () => {
    const out = collapse([item('income', 'a', 1), item('income', 'b', 3)], 'Other')
    expect(out.map((c) => c.key)).toEqual(['b', 'a'])
  })
})
