import type { WealthCashForecast, WealthCashForecastPoint } from '@/types/wealth'

export type ForecastTitleKind = 'negative' | 'below_buffer' | 'ok'

export interface ForecastTitleInfo {
  kind: ForecastTitleKind
  /** ISO date the title names, set for the negative/below_buffer cases. */
  date: string | null
}

/**
 * Which takeaway title the forecast chart shows (plan S8). The first thing
 * going wrong wins: going negative is worse than merely dipping below the
 * buffer, and either beats the generic "N days of runway" framing when
 * nothing is wrong.
 */
export function forecastTitleInfo(forecast: WealthCashForecast): ForecastTitleInfo {
  if (forecast.first_negative_date) return { kind: 'negative', date: forecast.first_negative_date }
  if (forecast.first_below_buffer_date) return { kind: 'below_buffer', date: forecast.first_below_buffer_date }
  return { kind: 'ok', date: null }
}

/** Defensive sort: the chart and its table fallback both assume chronological order. */
export function sortedForecastPoints(points: WealthCashForecastPoint[]): WealthCashForecastPoint[] {
  return [...points].sort((a, b) => a.date.localeCompare(b.date))
}
