import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import type { UseQueryResult } from '@tanstack/react-query'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { ChartCard, type ChartCardTable } from '@/components/charts/ChartCard'
import { ChartTooltip } from '@/components/charts/ChartTooltip'
import { MaskedAxisTick } from '@/components/charts/MaskedAxisTick'
import { Button } from '@/components/ui/button'
import { useChartTheme } from '@/hooks/use-chart-theme'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { formatMoney } from '@/lib/chart-format'
import type { WealthCashForecast } from '@/types/wealth'

import { formatCashDay } from './cash-format'
import { forecastTitleInfo, sortedForecastPoints } from './cash-forecast-data'

export interface CashForecastChartProps {
  query: UseQueryResult<WealthCashForecast>
}

/**
 * The 90-day cash forecast (plan B5b/S8): a step line of the projected cash
 * balance, with reference lines at the buffer and at $0 and the low point
 * marked. The takeaway title names the first thing going wrong -- going
 * negative, then dipping below the buffer -- before falling back to the
 * "stays above your buffer" framing.
 */
export function CashForecastChart({ query }: CashForecastChartProps) {
  const { t } = useTranslation()

  if (query.isError) {
    return (
      <ChartCard title={t('wealthCash.forecast.titleLoading')}>
        <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-destructive">
          <span>{t('common.error')}</span>
          <Button variant="outline" size="sm" onClick={() => query.refetch()}>
            {t('common.retry')}
          </Button>
        </div>
      </ChartCard>
    )
  }

  if (!query.data) {
    return (
      <ChartCard title={t('wealthCash.forecast.titleLoading')} loading>
        <></>
      </ChartCard>
    )
  }

  return <CashForecastChartBody forecast={query.data} />
}

function CashForecastChartBody({ forecast }: { forecast: WealthCashForecast }) {
  const { t } = useTranslation()
  const theme = useChartTheme()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()

  const rows = useMemo(() => sortedForecastPoints(forecast.points), [forecast.points])
  const titleInfo = forecastTitleInfo(forecast)

  const title =
    titleInfo.kind === 'negative'
      ? t('wealthCash.forecast.title.negative', { date: formatCashDay(titleInfo.date ?? '', locale) })
      : titleInfo.kind === 'below_buffer'
        ? t('wealthCash.forecast.title.belowBuffer', { date: formatCashDay(titleInfo.date ?? '', locale) })
        : t('wealthCash.forecast.title.ok', { days: forecast.days })

  const table: ChartCardTable = {
    columns: [
      { key: 'date', label: t('wealthCash.forecast.table.date') },
      { key: 'balance', label: t('wealthCash.forecast.table.balance') },
      { key: 'inflow', label: t('wealthCash.forecast.table.inflow') },
      { key: 'outflow', label: t('wealthCash.forecast.table.outflow') },
    ],
    rows: rows.map((point) => ({
      id: point.date,
      date: formatCashDay(point.date, locale),
      balance: mask(formatMoney(point.balance, forecast.currency, { locale })),
      inflow: mask(formatMoney(point.inflow, forecast.currency, { locale })),
      outflow: mask(formatMoney(point.outflow, forecast.currency, { locale })),
    })),
  }

  return (
    <ChartCard title={title} footnote={t('wealthCash.forecast.footnote')} table={table} empty={rows.length === 0}>
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={rows} margin={{ top: 8, right: 12, left: 0, bottom: 16 }}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} stroke={theme.grid} strokeOpacity={0.6} />
          <XAxis
            dataKey="date"
            tickFormatter={(value: string) => formatCashDay(value, locale)}
            tick={{ fontSize: 10, fill: theme.axis }}
            axisLine={false}
            tickLine={false}
          />
          <YAxis
            tick={<MaskedAxisTick currency={forecast.currency} />}
            axisLine={false}
            tickLine={false}
            width={56}
          />
          <Tooltip
            content={
              <ChartTooltip
                currency={forecast.currency}
                labelFormatter={(value) => formatCashDay(String(value), locale)}
              />
            }
          />
          <ReferenceLine y={0} stroke={theme.axis} />
          <ReferenceLine
            y={forecast.buffer}
            stroke={theme.grid}
            strokeDasharray="4 4"
            label={{
              value: t('wealthCash.forecast.bufferLabel'),
              position: 'insideTopLeft',
              fill: theme.axis,
              fontSize: 10,
            }}
          />
          {forecast.low_point && (
            <ReferenceDot
              x={forecast.low_point.date}
              y={forecast.low_point.balance}
              r={4}
              fill={theme.negative}
              stroke="none"
              label={{
                value: t('wealthCash.forecast.lowPointLabel', {
                  amount: mask(formatMoney(forecast.low_point.balance, forecast.currency, { compact: true, locale })),
                }),
                position: 'bottom',
                fill: theme.axis,
                fontSize: 10,
              }}
            />
          )}
          <Line
            type="stepAfter"
            dataKey="balance"
            name={t('wealthCash.forecast.balanceLabel')}
            stroke={theme.series[4]}
            strokeWidth={2.5}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </ChartCard>
  )
}
