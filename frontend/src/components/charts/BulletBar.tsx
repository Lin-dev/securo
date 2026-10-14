import { useTranslation } from 'react-i18next'

import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { useChartTheme } from '@/hooks/use-chart-theme'
import { formatMoney } from '@/lib/chart-format'

export interface BulletBarBand {
  /** Upper bound of this qualitative band, on the same scale as `value`/`target`. */
  limit: number
  label?: string
}

export interface BulletBarProps {
  value: number
  target: number
  /** A second reference tick, e.g. the pace needed to hit the target on time. */
  marker?: number
  markerLabel?: string
  /** Qualitative bands (e.g. poor/ok/good), drawn back to front from 0. */
  bands?: BulletBarBand[]
  currency?: string
  height?: number
  className?: string
}

/**
 * A horizontal bullet bar: how `value` sits against `target`, an optional
 * `marker` (e.g. pace) and qualitative bands. Pure HTML/CSS — no recharts.
 * The bar is `role="img"` with every number baked into its `aria-label`
 * (masked in privacy mode); a visible caption underneath carries the same
 * numbers for sighted users. Target and marker are drawn as different tick
 * shapes (solid vs. dashed), not distinguished by colour alone.
 */
export function BulletBar({
  value,
  target,
  marker,
  markerLabel,
  bands = [],
  currency = 'USD',
  height = 28,
  className,
}: BulletBarProps) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const theme = useChartTheme()

  const scaleMax = Math.max(value, target, marker ?? 0, ...bands.map((b) => b.limit), 1)
  const toPct = (n: number) => `${Math.min(100, Math.max(0, (n / scaleMax) * 100))}%`

  const maskedValue = mask(formatMoney(value, currency))
  const maskedTarget = mask(formatMoney(target, currency))
  const ariaLabel = t('charts.bulletBarLabel', { value: maskedValue, target: maskedTarget })

  const bandRects = bands.reduce<Array<BulletBarBand & { left: string; width: string }>>(
    (acc, band) => {
      const bandStart = acc.length > 0 ? bands[acc.length - 1].limit : 0
      const left = toPct(bandStart)
      const width = toPct(Math.max(0, band.limit - bandStart))
      acc.push({ ...band, left, width })
      return acc
    },
    [],
  )

  return (
    <div className={className}>
      <div
        role="img"
        aria-label={ariaLabel}
        className="relative w-full overflow-hidden rounded-md"
        style={{ height, backgroundColor: theme.grid }}
      >
        {bandRects.map((band, i) => (
          <div
            key={band.label ?? i}
            aria-hidden="true"
            className="absolute inset-y-0"
            style={{ left: band.left, width: band.width, backgroundColor: theme.grid, opacity: 0.4 + i * 0.18 }}
          />
        ))}
        <div
          aria-hidden="true"
          className="absolute inset-y-1 left-0 rounded-sm"
          style={{ width: toPct(value), backgroundColor: 'var(--primary)' }}
        />
        <div
          aria-hidden="true"
          className="absolute inset-y-0 w-0.5"
          style={{ left: toPct(target), backgroundColor: theme.axis }}
        />
        {marker != null && (
          <div
            aria-hidden="true"
            className="absolute inset-y-0 w-0.5 border-l-2 border-dashed"
            style={{ left: toPct(marker), borderColor: theme.axis }}
          />
        )}
      </div>
      <div className="mt-1 flex items-center justify-between gap-2 text-[11px] text-muted-foreground tabular-nums">
        <span>{maskedValue}</span>
        {marker != null && (
          <span>
            {markerLabel ?? t('charts.marker')}: {mask(formatMoney(marker, currency))}
          </span>
        )}
        <span>
          {t('charts.target')}: {maskedTarget}
        </span>
      </div>
    </div>
  )
}
