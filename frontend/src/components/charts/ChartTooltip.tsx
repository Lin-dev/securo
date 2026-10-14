import type { ReactNode } from 'react'

import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { formatMoney, formatPct } from '@/lib/chart-format'

export type ChartTooltipValueType = 'money' | 'percent'

export interface ChartTooltipEntry {
  color?: string
  name?: ReactNode
  value?: number | string | null
  dataKey?: string | number
}

export interface ChartTooltipProps {
  /** recharts passes these when it mounts this as a `<Tooltip content={...} />`. */
  active?: boolean
  label?: string | number
  payload?: ChartTooltipEntry[]
  currency?: string
  /** Value type applied to every row unless overridden in `seriesTypes`. Defaults to "money". */
  valueType?: ChartTooltipValueType
  /** Per-series override keyed by `dataKey`, for charts that mix money and percent series. */
  seriesTypes?: Record<string, ChartTooltipValueType>
  compact?: boolean
  /** Fraction digits for percent rows. */
  digits?: number
  labelFormatter?: (label: string | number | undefined) => ReactNode
}

/**
 * recharts tooltip content, styled like the app's existing ad-hoc tooltips
 * (see pages/dashboard.tsx, pages/reports.tsx) but shared: formats money and
 * percent through `lib/chart-format`, and masks every value in privacy mode.
 */
export function ChartTooltip({
  active,
  label,
  payload,
  currency = 'USD',
  valueType = 'money',
  seriesTypes,
  compact = false,
  digits,
  labelFormatter,
}: ChartTooltipProps) {
  const { mask } = usePrivacyMode()

  if (!active || !payload || payload.length === 0) return null

  return (
    <div
      className="rounded-xl border px-3 py-2 text-xs shadow-lg"
      style={{
        background: 'var(--card)',
        color: 'var(--foreground)',
        borderColor: 'var(--border)',
      }}
    >
      {label != null && (
        <p className="mb-1 font-medium text-foreground">
          {labelFormatter ? labelFormatter(label) : label}
        </p>
      )}
      <div className="flex flex-col gap-0.5">
        {payload.map((entry, i) => {
          const key = entry.dataKey != null ? String(entry.dataKey) : undefined
          const type = (key && seriesTypes?.[key]) ?? valueType
          const numericValue = typeof entry.value === 'number' ? entry.value : Number(entry.value)
          const formatted = Number.isFinite(numericValue)
            ? type === 'percent'
              ? formatPct(numericValue, digits)
              : formatMoney(numericValue, currency, { compact, signed: false })
            : String(entry.value ?? '—')

          return (
            <div key={key ?? i} className="flex items-center gap-2 tabular-nums">
              {entry.color && (
                <span
                  aria-hidden="true"
                  className="inline-block h-2 w-2 shrink-0 rounded-full"
                  style={{ backgroundColor: entry.color }}
                />
              )}
              <span className="text-muted-foreground">{entry.name}</span>
              <span className="ml-auto font-medium">{mask(formatted)}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
