import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { useChartTheme } from '@/hooks/use-chart-theme'
import { formatMonth } from '@/lib/chart-format'
import { ChartTooltip } from '@/components/charts/ChartTooltip'
import { MaskedAxisTick } from '@/components/charts/MaskedAxisTick'

export interface RangeLinePoint {
  /** ISO date, e.g. "2026-11-01". */
  date: string
  value: number
}

export interface RangeLinesProps {
  low: RangeLinePoint[]
  base: RangeLinePoint[]
  high: RangeLinePoint[]
  actual?: RangeLinePoint[]
  /** A horizontal reference line, e.g. the $1M milestone. */
  target?: number
  targetLabel?: string
  currency?: string
  height?: number
}

interface MergedRow {
  date: string
  low?: number
  base?: number
  high?: number
  actual?: number
}

function mergeByDate(low: RangeLinePoint[], base: RangeLinePoint[], high: RangeLinePoint[], actual?: RangeLinePoint[]): MergedRow[] {
  const rows = new Map<string, MergedRow>()
  const put = (points: RangeLinePoint[], key: keyof Omit<MergedRow, 'date'>) => {
    for (const p of points) {
      const row = rows.get(p.date) ?? { date: p.date }
      row[key] = p.value
      rows.set(p.date, row)
    }
  }
  put(low, 'low')
  put(base, 'base')
  put(high, 'high')
  if (actual) put(actual, 'actual')
  return [...rows.values()].sort((a, b) => a.date.localeCompare(b.date))
}

/**
 * Low/base/high projection fan, plus an optional actual series and target
 * line. Series are told apart by dash pattern and label (P8) — never by
 * colour alone, since low/base/high are a single conceptual band rather than
 * unrelated categories.
 */
export function RangeLines({
  low,
  base,
  high,
  actual,
  target,
  targetLabel,
  currency = 'USD',
  height = 240,
}: RangeLinesProps) {
  const { t } = useTranslation()
  const theme = useChartTheme()
  const data = useMemo(() => mergeByDate(low, base, high, actual), [low, base, high, actual])

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" vertical={false} stroke={theme.grid} strokeOpacity={0.6} />
        <XAxis
          dataKey="date"
          tickFormatter={(value: string) => formatMonth(value)}
          tick={{ fontSize: 10, fill: theme.axis }}
          axisLine={false}
          tickLine={false}
        />
        <YAxis
          tick={<MaskedAxisTick currency={currency} />}
          axisLine={false}
          tickLine={false}
          width={56}
        />
        <Tooltip
          content={
            <ChartTooltip
              currency={currency}
              labelFormatter={(value) => formatMonth(String(value))}
            />
          }
        />
        {target != null && (
          <ReferenceLine
            y={target}
            stroke={theme.axis}
            strokeDasharray="4 4"
            label={{ value: targetLabel ?? t('charts.target'), position: 'insideTopLeft', fill: theme.axis, fontSize: 11 }}
          />
        )}
        <Line
          type="monotone"
          dataKey="low"
          name={t('charts.low')}
          stroke={theme.series[1]}
          strokeWidth={1.5}
          strokeDasharray="4 3"
          dot={false}
          connectNulls
          isAnimationActive={false}
        />
        <Line
          type="monotone"
          dataKey="high"
          name={t('charts.high')}
          stroke={theme.series[1]}
          strokeWidth={1.5}
          strokeDasharray="2 4"
          dot={false}
          connectNulls
          isAnimationActive={false}
        />
        <Line
          type="monotone"
          dataKey="base"
          name={t('charts.base')}
          stroke={theme.series[4]}
          strokeWidth={2.5}
          dot={false}
          connectNulls
          isAnimationActive={false}
        />
        {actual && (
          <Line
            type="monotone"
            dataKey="actual"
            name={t('charts.actual')}
            stroke={theme.positive}
            strokeWidth={2}
            strokeDasharray="1 3"
            dot={{ r: 2 }}
            connectNulls
            isAnimationActive={false}
          />
        )}
      </LineChart>
    </ResponsiveContainer>
  )
}
