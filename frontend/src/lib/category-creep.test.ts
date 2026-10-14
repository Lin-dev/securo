import { describe, expect, it } from 'vitest'

import { categoriesCreepingUp, categorySpikes, type MonthCatsMap } from '@/lib/category-creep'

// Mirrors `backend/tests/test_agents_advisor_handlers.py::_months`.
function months(valuesByMonth: Record<number, Record<string, number>>): MonthCatsMap {
  const out: MonthCatsMap = {}
  for (const [m, v] of Object.entries(valuesByMonth)) {
    out[`2026-${String(m).padStart(2, '0')}`] = v
  }
  return out
}

// Ports `test_category_spikes_edges_and_short_history` exactly.
describe('categorySpikes', () => {
  const base = Object.fromEntries([3, 4, 5, 6, 7, 8].map((m) => [m, { Travel: 400.0 }]))

  it('does not flag a month at 1.49x its baseline', () => {
    expect(categorySpikes(months({ ...base, 9: { Travel: 596.0 } }), '2026-09')).toEqual([])
  })

  it('flags a month at 1.51x its baseline, over by 200', () => {
    const spikes = categorySpikes(months({ ...base, 9: { Travel: 604.0 } }), '2026-09')
    expect(spikes).toEqual([{ category: 'Travel', amount: 604.0, baseline: 400.0, excess: 204.0 }])
  })

  it('never flags Uncategorized', () => {
    expect(categorySpikes(months({ ...base, 9: { Uncategorized: 5000.0 } }), '2026-09')).toEqual([])
  })

  it('returns null with fewer than 3 months of history', () => {
    const data = months({ 7: { Travel: 400.0 }, 8: { Travel: 400.0 }, 9: { Travel: 999.0 } })
    expect(categorySpikes(data, '2026-09')).toBeNull()
  })

  it('skips a category with fewer than 3 nonzero baseline months', () => {
    const sparse = months({
      3: {}, 4: {}, 5: {}, 6: {},
      7: { Gifts: 300.0 }, 8: { Gifts: 300.0 }, 9: { Gifts: 900.0 },
    })
    expect(categorySpikes(sparse, '2026-09')).toEqual([])
  })

  it('sorts multiple spikes largest excess first', () => {
    const data = months({
      ...Object.fromEntries([3, 4, 5, 6, 7, 8].map((m) => [m, { Travel: 400.0, Gifts: 100.0 }])),
      9: { Travel: 650.0, Gifts: 400.0 },
    })
    expect(categorySpikes(data, '2026-09')).toEqual([
      { category: 'Gifts', amount: 400.0, baseline: 100.0, excess: 300.0 },
      { category: 'Travel', amount: 650.0, baseline: 400.0, excess: 250.0 },
    ])
  })
})

describe('categoriesCreepingUp', () => {
  function series(byMonth: Record<string, number>) {
    return Object.entries(byMonth).map(([date, value]) => ({ date, value }))
  }

  it('finds the latest complete month strictly before the current one', () => {
    const categories = [
      {
        key: 'travel', label: 'Travel', color: '#F43F5E',
        series: series({
          '2026-03': 400, '2026-04': 400, '2026-05': 400, '2026-06': 400, '2026-07': 400, '2026-08': 400,
          '2026-09': 604,
          // October is the current, still-open month -- never the target.
          '2026-10': 50,
        }),
      },
    ]
    const result = categoriesCreepingUp(categories, '2026-10')
    expect(result).toEqual([
      { key: 'travel', label: 'Travel', color: '#F43F5E', month: '2026-09', amount: 604, baseline: 400, excess: 204 },
    ])
  })

  it('returns null when only the current month has data', () => {
    const categories = [
      { key: 'travel', label: 'Travel', color: '#F43F5E', series: series({ '2026-10': 50 }) },
    ]
    expect(categoriesCreepingUp(categories, '2026-10')).toBeNull()
  })

  it('returns an empty list when nothing is creeping up', () => {
    const categories = [
      {
        key: 'travel', label: 'Travel', color: '#F43F5E',
        series: series({
          '2026-03': 400, '2026-04': 400, '2026-05': 400, '2026-06': 400, '2026-07': 400, '2026-08': 400,
          '2026-09': 410,
        }),
      },
    ]
    expect(categoriesCreepingUp(categories, '2026-10')).toEqual([])
  })

  it('skips an Uncategorized series entirely', () => {
    const categories = [
      {
        key: 'uncategorized', label: 'Uncategorized', color: '#9CA3AF',
        series: series({
          '2026-03': 10, '2026-04': 10, '2026-05': 10, '2026-06': 10, '2026-07': 10, '2026-08': 10,
          '2026-09': 5000,
        }),
      },
    ]
    expect(categoriesCreepingUp(categories, '2026-10')).toEqual([])
  })

  it('accumulates multiple points landing in the same month', () => {
    const categories = [
      {
        key: 'travel', label: 'Travel', color: '#F43F5E',
        series: [
          ...series({ '2026-03': 400, '2026-04': 400, '2026-05': 400, '2026-06': 400, '2026-07': 400, '2026-08': 400 }),
          { date: '2026-09-10', value: 300 },
          { date: '2026-09-20', value: 304 },
        ],
      },
    ]
    const result = categoriesCreepingUp(categories, '2026-10')
    expect(result).toEqual([
      { key: 'travel', label: 'Travel', color: '#F43F5E', month: '2026-09', amount: 604, baseline: 400, excess: 204 },
    ])
  })
})
