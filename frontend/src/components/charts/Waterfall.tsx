import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { useChartTheme } from '@/hooks/use-chart-theme'
import { formatMoney } from '@/lib/chart-format'
import { MaskedAxisTick } from '@/components/charts/MaskedAxisTick'

export interface WaterfallStep {
  label: string
  value: number
}

export interface WaterfallProps {
  steps: WaterfallStep[]
  currency?: string
  height?: number
  /** Defaults to the translated "Total". */
  totalLabel?: string
}

interface WaterfallRow {
  label: string
  value: number
  /** Invisible spacer bar height, so the visible "delta" bar floats at the right height. */
  base: number
  /** Visible bar height — always positive; sign lives in colour + the value label. */
  delta: number
  isNegative: boolean
  isTotal: boolean
}

function buildRows(steps: WaterfallStep[], totalLabel: string): WaterfallRow[] {
  let running = 0
  const rows: WaterfallRow[] = steps.map((step) => {
    const start = running
    running += step.value
    return {
      label: step.label,
      value: step.value,
      base: Math.min(start, running),
      delta: Math.abs(step.value),
      isNegative: step.value < 0,
      isTotal: false,
    }
  })
  rows.push({
    label: totalLabel,
    value: running,
    base: 0,
    delta: Math.abs(running),
    isNegative: running < 0,
    isTotal: true,
  })
  return rows
}

interface WaterfallTooltipProps {
  active?: boolean
  payload?: Array<{ payload: WaterfallRow }>
  currency: string
  mask: (value: string) => string
}

function WaterfallTooltip({ active, payload, currency, mask }: WaterfallTooltipProps) {
  if (!active || !payload || payload.length === 0) return null
  const row = payload[0].payload
  return (
    <div
      className="rounded-xl border px-3 py-2 text-xs shadow-lg"
      style={{ background: 'var(--card)', color: 'var(--foreground)', borderColor: 'var(--border)' }}
    >
      <p className="mb-0.5 font-medium text-foreground">{row.label}</p>
      <p className="tabular-nums">{mask(formatMoney(row.value, currency, { signed: true }))}</p>
    </div>
  )
}

/**
 * A running-total waterfall: each step floats from the previous cumulative
 * total to the next, with an appended "Total" bar. Negative steps get both
 * a different colour (P8: colour never carries meaning alone) AND a "-"
 * sign glyph baked into the value label via `formatMoney`'s `signed` option.
 */
export function Waterfall({ steps, currency = 'USD', height = 260, totalLabel }: WaterfallProps) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const theme = useChartTheme()

  const resolvedTotalLabel = totalLabel ?? t('charts.total')
  const rows = useMemo(() => buildRows(steps, resolvedTotalLabel), [steps, resolvedTotalLabel])

  const colorFor = (row: WaterfallRow) => {
    if (row.isTotal) return theme.series[4]
    return row.isNegative ? theme.negative : theme.positive
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={rows} margin={{ top: 20, right: 12, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" vertical={false} stroke={theme.grid} strokeOpacity={0.6} />
        <XAxis
          dataKey="label"
          tick={{ fontSize: 10, fill: theme.axis }}
          axisLine={false}
          tickLine={false}
        />
        <YAxis tick={<MaskedAxisTick currency={currency} />} axisLine={false} tickLine={false} width={56} />
        <ReferenceLine y={0} stroke={theme.axis} />
        <Tooltip content={<WaterfallTooltip currency={currency} mask={mask} />} />
        <Bar dataKey="base" stackId="waterfall" fill="transparent" isAnimationActive={false} />
        <Bar dataKey="delta" stackId="waterfall" isAnimationActive={false} radius={2}>
          {rows.map((row, i) => (
            <Cell key={row.label + i} fill={colorFor(row)} />
          ))}
          <LabelList
            dataKey="value"
            position="top"
            fontSize={11}
            fill={theme.axis}
            formatter={(value: unknown) => mask(formatMoney(Number(value), currency, { signed: true }))}
          />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}
