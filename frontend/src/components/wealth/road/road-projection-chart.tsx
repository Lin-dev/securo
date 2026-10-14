import type { UseQueryResult } from '@tanstack/react-query'
import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'

import { ChartCard } from '@/components/charts/ChartCard'
import { RangeLines } from '@/components/charts/RangeLines'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { formatMonth, formatMoney, formatPct, formatQuarter } from '@/lib/chart-format'
import type { WealthNetWorthHistory, WealthProjection } from '@/types/wealth'

import { RoadSectionError } from './road-section-state'
import { buildProjectionTableRows, historyToRangePoints, projectionPathToNetWorth } from './road-chart'

export interface RoadProjectionChartProps {
  projectionQuery: UseQueryResult<WealthProjection>
  historyQuery: UseQueryResult<WealthNetWorthHistory>
}

/**
 * The range chart: low/base/high net-worth paths, the actual history and
 * the $1M target line. The history query is a "nice to have" here — it
 * fails independently, and the chart still renders the projection without
 * an actual line when it does (plan: one failing query doesn't blank the
 * others).
 */
export function RoadProjectionChart({ projectionQuery, historyQuery }: RoadProjectionChartProps) {
  const { t } = useTranslation()

  if (projectionQuery.isError) {
    return (
      <ChartCard title={t('wealthRoad.chart.titleLoading')}>
        <RoadSectionError onRetry={() => projectionQuery.refetch()} />
      </ChartCard>
    )
  }

  if (!projectionQuery.data) {
    return (
      <ChartCard title={t('wealthRoad.chart.titleLoading')} loading>
        <></>
      </ChartCard>
    )
  }

  return <RoadProjectionChartBody projection={projectionQuery.data} history={historyQuery.data} />
}

function RoadProjectionChartBody({
  projection,
  history,
}: {
  projection: WealthProjection
  history: WealthNetWorthHistory | undefined
}) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()
  const { inputs, scenarios, precision, currency } = projection

  const low = useMemo(
    () => projectionPathToNetWorth(scenarios.low.path, inputs.non_invested),
    [scenarios.low.path, inputs.non_invested],
  )
  const base = useMemo(
    () => projectionPathToNetWorth(scenarios.base.path, inputs.non_invested),
    [scenarios.base.path, inputs.non_invested],
  )
  const high = useMemo(
    () => projectionPathToNetWorth(scenarios.high.path, inputs.non_invested),
    [scenarios.high.path, inputs.non_invested],
  )
  const actual = useMemo(() => (history ? historyToRangePoints(history.points) : undefined), [history])

  const money = (value: number) => formatMoney(value, currency, { locale })
  const title = scenarios.base.label
    ? t('wealthRoad.chart.title', { amount: money(inputs.target_total), label: scenarios.base.label })
    : t('wealthRoad.chart.titleUnreachable', { amount: money(inputs.target_total) })

  const footnote = t('wealthRoad.chart.footnote', {
    low: formatPct(scenarios.low.annual_return, 1, locale),
    base: formatPct(scenarios.base.annual_return, 1, locale),
    high: formatPct(scenarios.high.annual_return, 1, locale),
    days: inputs.coverage.days,
  })

  const formatRowMonth = (month: string) => (precision === 'quarter' ? formatQuarter(month) : formatMonth(month, locale))
  const rows = useMemo(() => buildProjectionTableRows(low, base, high), [low, base, high])
  const table = {
    columns: [
      { key: 'month', label: t(precision === 'quarter' ? 'wealthRoad.chart.columnQuarter' : 'wealthRoad.chart.columnMonth') },
      { key: 'low', label: t('charts.low') },
      { key: 'base', label: t('charts.base') },
      { key: 'high', label: t('charts.high') },
    ],
    rows: rows.map((row) => ({
      id: row.id,
      month: formatRowMonth(row.month),
      low: mask(formatMoney(row.low, currency, { locale })),
      base: mask(formatMoney(row.base, currency, { locale })),
      high: mask(formatMoney(row.high, currency, { locale })),
    })),
  }

  return (
    <ChartCard title={title} footnote={footnote} table={table}>
      <RangeLines low={low} base={base} high={high} actual={actual} target={inputs.target_total} currency={currency} />
    </ChartCard>
  )
}
