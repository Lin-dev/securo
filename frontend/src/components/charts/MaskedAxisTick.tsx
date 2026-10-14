import type { SVGProps } from 'react'

import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { formatMoney, formatPct } from '@/lib/chart-format'

export interface MaskedAxisTickPayload {
  value?: number | string
}

export interface MaskedAxisTickProps {
  /** recharts positions the tick by passing these through. */
  x?: number
  y?: number
  dy?: number
  textAnchor?: SVGProps<SVGTextElement>['textAnchor']
  payload?: MaskedAxisTickPayload
  currency?: string
  valueType?: 'money' | 'percent'
  /** Defaults to true: axis gutters are tight, so "$1.2K" beats "$1,234.00". */
  compact?: boolean
  digits?: number
  fill?: string
  fontSize?: number
}

/**
 * recharts axis tick (`<XAxis tick={<MaskedAxisTick />} />`): formats the
 * tick's numeric value through `lib/chart-format` and masks it in privacy
 * mode, same as `ChartTooltip`.
 */
export function MaskedAxisTick({
  x = 0,
  y = 0,
  dy = 0,
  textAnchor = 'middle',
  payload,
  currency = 'USD',
  valueType = 'money',
  compact = true,
  digits,
  fill = 'var(--chart-axis)',
  fontSize = 10,
}: MaskedAxisTickProps) {
  const { mask } = usePrivacyMode()

  const raw = payload?.value
  const numericValue = typeof raw === 'number' ? raw : Number(raw)
  const formatted = Number.isFinite(numericValue)
    ? valueType === 'percent'
      ? formatPct(numericValue, digits)
      : formatMoney(numericValue, currency, { compact })
    : String(raw ?? '')

  return (
    <text x={x} y={y} dy={dy} textAnchor={textAnchor} fill={fill} fontSize={fontSize}>
      {mask(formatted)}
    </text>
  )
}
