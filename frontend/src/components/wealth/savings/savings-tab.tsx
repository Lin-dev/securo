import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import {
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { BarRectangleItem } from 'recharts'

import { ChartCard, type ChartCardTable } from '@/components/charts/ChartCard'
import { ChartTooltip } from '@/components/charts/ChartTooltip'
import { MaskedAxisTick } from '@/components/charts/MaskedAxisTick'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { useChartTheme } from '@/hooks/use-chart-theme'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { wealth } from '@/lib/api'
import { formatMoney, formatMonth, formatPct } from '@/lib/chart-format'
import { monthWindow } from '@/lib/chart-period'
import { transactionsUrl } from '@/lib/drilldown'

import {
  SAVINGS_RATE_BANDS,
  SAVINGS_RATE_CLIP_BOUND,
  domainMax,
  domainMin,
  isClipped,
  latestMonthRate,
  latestRollingRate,
  toChartRows,
  visibleMonths,
  type SavingsChartRow,
} from './savings-data'

const MONTHS = 12

/**
 * The savings tab of /wealth (plan P1, S2): monthly bars of the savings rate
 * with a 3-month rolling line and faint reference bands at 20/35/50%. A
 * toggle switches the bars between the P1 rate ("incl. payroll
 * contributions") and the cash-only rate, since the user has seen the old
 * 16% ledger figure and the P1 definition reads closer to 24% for the same
 * months. Clicking a bar (or its table row) drills into Transactions for
 * that month.
 */
export function SavingsTab() {
  const { t } = useTranslation()
  const theme = useChartTheme()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()
  const navigate = useNavigate()
  const [useCashRate, setUseCashRate] = useState(false)
  const [showEarlier, setShowEarlier] = useState(false)

  const query = useQuery({
    queryKey: ['wealth', 'savings-rate', MONTHS],
    queryFn: () => wealth.savingsRate(MONTHS),
  })

  const series = query.data
  const currency = series?.currency ?? 'USD'
  // The table always lists every month (the ground truth); the chart -- its
  // bars, rolling line and takeaway title -- defaults to `visible` so a
  // handful of stray pre-coverage rows can't dominate it (see `visibleMonths`).
  const months = useMemo(() => series?.months ?? [], [series])
  const hasEarlierMonths = useMemo(
    () => months.some((month) => !month.in_coverage && !month.partial),
    [months],
  )
  const visible = useMemo(() => visibleMonths(months, showEarlier), [months, showEarlier])

  const rows = useMemo(() => toChartRows(visible, useCashRate), [visible, useCashRate])
  const rolling = useMemo(() => latestRollingRate(visible), [visible])
  const fallbackRate = useMemo(() => latestMonthRate(visible, useCashRate), [visible, useCashRate])

  function goToMonth(month: string) {
    navigate(transactionsUrl(monthWindow(month)))
  }

  function handleBarClick(data: BarRectangleItem) {
    const row = data.payload as SavingsChartRow | undefined
    if (row) goToMonth(row.month)
  }

  const title =
    rolling != null
      ? t('wealthSavings.title.rolling', { rate: mask(formatPct(rolling.value, 0, locale)) })
      : fallbackRate != null
        ? t('wealthSavings.title.fallback', { rate: mask(formatPct(fallbackRate, 0, locale)) })
        : t('wealthSavings.title.empty')

  const rateLabel = t(useCashRate ? 'wealthSavings.cashRateLabel' : 'wealthSavings.rateLabel')
  const barColor = theme.series[useCashRate ? 1 : 0]

  const showPartialNote = rows.some((row) => row.partial)
  const showCoverageNote = rows.some((row) => !row.inCoverage)
  const clippedRows = rows.filter((row) => isClipped(row.value))
  const showClippedNote = clippedRows.length > 0

  const table: ChartCardTable = {
    columns: [
      { key: 'month', label: t('wealthSavings.table.month') },
      { key: 'income', label: t('wealthSavings.table.income') },
      { key: 'expenses', label: t('wealthSavings.table.expenses') },
      { key: 'direct', label: t('wealthSavings.table.direct') },
      { key: 'rate', label: t('wealthSavings.table.rate') },
      { key: 'cashRate', label: t('wealthSavings.table.cashRate') },
      { key: 'rolling3m', label: t('wealthSavings.table.rolling3m') },
    ],
    rows: months.map((month) => ({
      id: month.month,
      month: (
        <Link to={transactionsUrl(monthWindow(month.month))} className="font-medium text-primary hover:underline">
          {formatMonth(month.month, locale)}
          {month.partial && ` (${t('wealthSavings.partialBadge')})`}
          {!month.in_coverage && ` (${t('wealthSavings.outsideCoverageBadge')})`}
        </Link>
      ),
      income: mask(formatMoney(month.income, currency, { locale })),
      expenses: mask(formatMoney(month.expenses, currency, { locale })),
      direct: mask(formatMoney(month.direct, currency, { locale })),
      rate: mask(formatPct(month.rate, 1, locale)),
      cashRate: mask(formatPct(month.cash_rate, 1, locale)),
      rolling3m: mask(formatPct(month.rolling_3m, 1, locale)),
    })),
  }

  if (query.isError) {
    return (
      <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-destructive">
        <span>{t('common.error')}</span>
        <Button variant="outline" size="sm" onClick={() => query.refetch()}>
          {t('common.retry')}
        </Button>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Switch checked={useCashRate} onCheckedChange={setUseCashRate} aria-labelledby="savings-cash-toggle-label" />
          <span id="savings-cash-toggle-label">{t('wealthSavings.cashToggleLabel')}</span>
        </div>
        {hasEarlierMonths && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Switch
              checked={showEarlier}
              onCheckedChange={setShowEarlier}
              aria-labelledby="savings-earlier-toggle-label"
            />
            <span id="savings-earlier-toggle-label">{t('wealthSavings.showEarlierToggleLabel')}</span>
          </div>
        )}
        <span className="text-sm font-medium text-foreground">{rateLabel}</span>
      </div>
      <p className="text-xs text-muted-foreground">{t('wealthSavings.definitionNote')}</p>

      <ChartCard title={title} loading={query.isLoading} empty={!query.isLoading && rows.length === 0} table={table}>
        <ResponsiveContainer width="100%" height={300}>
          <ComposedChart data={rows} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} stroke={theme.grid} strokeOpacity={0.6} />
            <XAxis
              dataKey="month"
              tickFormatter={(value: string) => formatMonth(value, locale)}
              tick={{ fontSize: 10, fill: theme.axis }}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              tick={<MaskedAxisTick valueType="percent" compact={false} digits={0} />}
              axisLine={false}
              tickLine={false}
              width={48}
              allowDataOverflow
              domain={[domainMin, domainMax]}
            />
            <ReferenceLine y={0} stroke={theme.axis} />
            {SAVINGS_RATE_BANDS.map((band) => (
              <ReferenceLine
                key={band}
                y={band}
                stroke={theme.grid}
                strokeDasharray="4 4"
                label={{ value: formatPct(band, 0, locale), position: 'right', fill: theme.axis, fontSize: 10 }}
              />
            ))}
            {clippedRows.map((row) => (
              <ReferenceDot
                key={row.month}
                x={row.month}
                y={row.value! > 0 ? SAVINGS_RATE_CLIP_BOUND : -SAVINGS_RATE_CLIP_BOUND}
                r={0}
                fill="none"
                stroke="none"
                ifOverflow="visible"
                label={{
                  value: mask(formatPct(row.value, 0, locale)),
                  position: row.value! > 0 ? 'top' : 'bottom',
                  fill: theme.axis,
                  fontSize: 10,
                }}
              />
            ))}
            <Tooltip
              content={
                <ChartTooltip
                  currency={currency}
                  valueType="percent"
                  seriesTypes={{ value: 'percent', rolling3m: 'percent' }}
                  labelFormatter={(value) => formatMonth(String(value), locale)}
                />
              }
            />
            <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11 }} />
            <Bar
              dataKey="value"
              name={rateLabel}
              radius={[3, 3, 0, 0]}
              isAnimationActive={false}
              cursor="pointer"
              onClick={handleBarClick}
            >
              {rows.map((row) => (
                <Cell
                  key={row.month}
                  fill={barColor}
                  fillOpacity={!row.inCoverage ? 0.3 : row.partial ? 0.6 : 1}
                  stroke={row.partial ? theme.axis : 'none'}
                  strokeDasharray={row.partial ? '3 2' : undefined}
                />
              ))}
            </Bar>
            <Line
              type="monotone"
              dataKey="rolling3m"
              name={t('wealthSavings.rollingLabel')}
              stroke={theme.series[4]}
              strokeWidth={2.5}
              dot={{ r: 3 }}
              connectNulls
              isAnimationActive={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </ChartCard>

      {(showPartialNote || showCoverageNote || showClippedNote) && (
        <div className="space-y-1 text-xs text-muted-foreground">
          {showPartialNote && <p>{t('wealthSavings.flags.partial')}</p>}
          {showCoverageNote && <p>{t('wealthSavings.flags.outsideCoverage')}</p>}
          {showClippedNote && <p>{t('wealthSavings.flags.clipped')}</p>}
        </div>
      )}
    </div>
  )
}
