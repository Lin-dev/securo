import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { useChartTheme } from '@/hooks/use-chart-theme'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { wealth } from '@/lib/api'
import { formatMonth, formatMoney } from '@/lib/chart-format'
import { WEALTH_BUCKETS } from '@/types/wealth'

import {
  LIQUID_BUCKETS,
  formatDayLabel,
  hasFlag,
  netWorthDelta,
  toChartRows,
  visiblePoints,
} from './networth-data'

const MONTHS_OPTIONS = [6, 12, 24] as const
type MonthsOption = (typeof MONTHS_OPTIONS)[number]
const DEFAULT_MONTHS: MonthsOption = 12

function isMonthsOption(value: number): value is MonthsOption {
  return (MONTHS_OPTIONS as readonly number[]).includes(value)
}

/**
 * The net worth tab of /wealth (plan P2/P3, S2): a stacked area chart of the
 * nine buckets, with the two debt buckets plotted below zero
 * (`stackOffset="sign"`, proven in `stack-offset-sign.spike.test.tsx`), a
 * total-net-worth line on top, a "Liquid only" view and a toggle for the
 * estimated history that precedes the first holdings sync.
 */
export function NetWorthTab() {
  const { t } = useTranslation()
  const theme = useChartTheme()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()
  const [searchParams, setSearchParams] = useSearchParams()
  const [months, setMonths] = useState<MonthsOption>(DEFAULT_MONTHS)
  const [includeEstimated, setIncludeEstimated] = useState(false)

  const liquidOnly = searchParams.get('liquid') === '1'

  const query = useQuery({
    queryKey: ['wealth', 'net-worth-history', months],
    queryFn: () => wealth.netWorthHistory(months),
  })

  function setLiquidOnly(next: boolean) {
    const params = new URLSearchParams(searchParams)
    if (next) params.set('liquid', '1')
    else params.delete('liquid')
    setSearchParams(params, { replace: true })
  }

  function setMonthsOption(value: string) {
    const parsed = Number(value)
    if (isMonthsOption(parsed)) setMonths(parsed)
  }

  const history = query.data
  const currency = history?.currency ?? 'USD'
  const syncedFrom = history?.synced_from ?? null
  const hasSyncedPeriod = syncedFrom != null

  const shown = useMemo(
    () => (history ? visiblePoints(history.points, syncedFrom, includeEstimated) : []),
    [history, syncedFrom, includeEstimated],
  )
  const rows = useMemo(() => toChartRows(shown, syncedFrom), [shown, syncedFrom])
  const delta = useMemo(() => netWorthDelta(shown, liquidOnly), [shown, liquidOnly])

  const showEstimatedNote =
    Boolean(history) && (!hasSyncedPeriod || (includeEstimated && shown.some((p) => p.date < (syncedFrom ?? ''))))
  const showCashFlatNote = hasFlag(shown, 'cash_flat')
  const showNoHoldingsNote = hasFlag(shown, 'no_holdings')

  const label = liquidOnly ? t('wealthNetworth.liquidLabel') : t('wealthNetworth.label')
  const title = !delta
    ? t('wealthNetworth.titleEmpty', { label })
    : delta.amount > 0
      ? t('wealthNetworth.title.rose', {
          label,
          amount: mask(formatMoney(Math.abs(delta.amount), currency, { locale })),
          date: formatDayLabel(delta.fromDate, locale),
        })
      : delta.amount < 0
        ? t('wealthNetworth.title.fell', {
            label,
            amount: mask(formatMoney(Math.abs(delta.amount), currency, { locale })),
            date: formatDayLabel(delta.fromDate, locale),
          })
        : t('wealthNetworth.title.flat', { label, date: formatDayLabel(delta.fromDate, locale) })

  const activeBuckets = liquidOnly ? LIQUID_BUCKETS : WEALTH_BUCKETS
  const totalActualKey = liquidOnly ? 'liquidActual' : 'totalActual'
  const totalEstimatedKey = liquidOnly ? 'liquidEstimated' : 'totalEstimated'
  const totalName = liquidOnly ? t('wealthNetworth.legend.liquidTotal') : t('wealthNetworth.legend.total')

  const table: ChartCardTable = {
    columns: [
      { key: 'date', label: t('wealthNetworth.table.date') },
      ...WEALTH_BUCKETS.map((bucket) => ({ key: bucket, label: t(`wealthSettings.buckets.${bucket}`) })),
      { key: 'total', label: t('wealthNetworth.table.total') },
      { key: 'liquid', label: t('wealthNetworth.table.liquid') },
    ],
    rows: shown.map((point) => ({
      id: point.date,
      date: formatMonth(point.date, locale),
      ...WEALTH_BUCKETS.reduce<Record<string, string>>((acc, bucket) => {
        acc[bucket] = mask(formatMoney(point.buckets[bucket], currency, { locale }))
        return acc
      }, {}),
      total: mask(formatMoney(point.total, currency, { locale })),
      liquid: mask(formatMoney(point.liquid, currency, { locale })),
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
      <div className="flex flex-wrap items-center gap-4">
        <Select value={String(months)} onValueChange={setMonthsOption}>
          <SelectTrigger aria-label={t('wealthNetworth.monthsLabel')} className="w-36">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {MONTHS_OPTIONS.map((option) => (
              <SelectItem key={option} value={String(option)}>
                {t('wealthNetworth.monthsOption', { count: option })}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {hasSyncedPeriod && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Switch
              checked={includeEstimated}
              onCheckedChange={setIncludeEstimated}
              aria-labelledby="networth-estimated-label"
            />
            <span id="networth-estimated-label">{t('wealthNetworth.estimatedToggleLabel')}</span>
          </div>
        )}

        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Switch
            checked={liquidOnly}
            onCheckedChange={setLiquidOnly}
            aria-labelledby="networth-liquid-label"
          />
          <span id="networth-liquid-label">{t('wealthNetworth.liquidToggleLabel')}</span>
        </div>
      </div>

      <ChartCard
        title={title}
        subtitle={liquidOnly ? t('wealthNetworth.liquidHelp') : undefined}
        loading={query.isLoading}
        empty={!query.isLoading && rows.length === 0}
        table={table}
      >
        <ResponsiveContainer width="100%" height={300}>
          <ComposedChart data={rows} stackOffset="sign" margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} stroke={theme.grid} strokeOpacity={0.6} />
            <XAxis
              dataKey="date"
              tickFormatter={(value: string) => formatMonth(value, locale)}
              tick={{ fontSize: 10, fill: theme.axis }}
              axisLine={false}
              tickLine={false}
            />
            <YAxis tick={<MaskedAxisTick currency={currency} />} axisLine={false} tickLine={false} width={64} />
            <ReferenceLine y={0} stroke={theme.axis} />
            <Tooltip
              content={
                <ChartTooltip currency={currency} labelFormatter={(value) => formatMonth(String(value), locale)} />
              }
            />
            <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11 }} />
            {activeBuckets.map((bucket, i) => {
              // The kit ships 8 Okabe-Ito series colours (theme.series) for
              // P3's 9 buckets, so one pair must share. Clamping (rather than
              // wrapping with `%`) makes the 9th bucket (other_debt) land on
              // the same colour as the 8th (card_debt) -- both debt, both
              // already set apart by sitting below the zero line -- instead
              // of silently colliding with `cash` at index 0.
              const color = theme.series[Math.min(i, theme.series.length - 1)]
              return (
                <Area
                  key={bucket}
                  type="monotone"
                  dataKey={bucket}
                  stackId="networth"
                  name={t(`wealthSettings.buckets.${bucket}`)}
                  stroke={color}
                  fill={color}
                  fillOpacity={0.55}
                  isAnimationActive={false}
                />
              )
            })}
            <Line
              type="monotone"
              dataKey={totalActualKey}
              name={totalName}
              stroke={theme.axis}
              strokeWidth={2.5}
              dot={false}
              connectNulls
              isAnimationActive={false}
            />
            <Line
              type="monotone"
              dataKey={totalEstimatedKey}
              name={t('wealthNetworth.legend.estimated')}
              stroke={theme.axis}
              strokeWidth={2}
              strokeDasharray="4 3"
              dot={false}
              connectNulls
              isAnimationActive={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </ChartCard>

      {(showEstimatedNote || showCashFlatNote || showNoHoldingsNote) && (
        <div className="space-y-1 text-xs text-muted-foreground">
          {showEstimatedNote && (
            <p>
              {hasSyncedPeriod
                ? t('wealthNetworth.flags.estimatedSince', { date: formatDayLabel(syncedFrom!, locale) })
                : t('wealthNetworth.flags.estimatedAll')}
            </p>
          )}
          {showCashFlatNote && <p>{t('wealthNetworth.flags.cashFlat')}</p>}
          {showNoHoldingsNote && <p>{t('wealthNetworth.flags.noHoldings')}</p>}
        </div>
      )}
    </div>
  )
}
