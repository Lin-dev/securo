import { describe, expect, it } from 'vitest'

import { wealthCashForecastFixture } from '@/test/fixtures/wealth'

import { forecastTitleInfo, sortedForecastPoints } from './cash-forecast-data'

describe('forecastTitleInfo', () => {
  it('reports "ok" when neither a negative nor a below-buffer date is set', () => {
    expect(forecastTitleInfo(wealthCashForecastFixture)).toEqual({ kind: 'ok', date: null })
  })

  it('reports "below_buffer" with its date when set', () => {
    const forecast = { ...wealthCashForecastFixture, first_below_buffer_date: '2026-11-12' }
    expect(forecastTitleInfo(forecast)).toEqual({ kind: 'below_buffer', date: '2026-11-12' })
  })

  it('prefers "negative" over "below_buffer" when both are set', () => {
    const forecast = {
      ...wealthCashForecastFixture,
      first_negative_date: '2026-12-01',
      first_below_buffer_date: '2026-11-12',
    }
    expect(forecastTitleInfo(forecast)).toEqual({ kind: 'negative', date: '2026-12-01' })
  })
})

describe('sortedForecastPoints', () => {
  it('sorts points chronologically without mutating the input', () => {
    const points = [
      { date: '2026-10-31', balance: 1, inflow: 0, outflow: 0 },
      { date: '2026-10-14', balance: 2, inflow: 0, outflow: 0 },
      { date: '2026-10-25', balance: 3, inflow: 0, outflow: 0 },
    ]
    const original = [...points]

    expect(sortedForecastPoints(points).map((p) => p.date)).toEqual(['2026-10-14', '2026-10-25', '2026-10-31'])
    expect(points).toEqual(original)
  })
})
