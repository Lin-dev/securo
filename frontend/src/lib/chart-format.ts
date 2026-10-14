/**
 * Formatters for the shared chart kit (components/charts/*).
 *
 * These are built on top of the canonical formatter in `lib/format.ts`
 * (`formatCurrency`) rather than re-implementing currency display: the
 * non-compact path calls it directly, and the compact path reuses its
 * locale/currency conventions while adding `Intl`'s `notation: "compact"`,
 * which `formatCurrency` does not expose.
 *
 * Every function here is pure — masking for privacy mode happens in the
 * components that call these, not in here.
 */
import { formatCurrency } from '@/lib/format'

export interface FormatMoneyOptions {
  /** "$1.2K" / "$3.4M" instead of the full "$1,234.00". For axis labels and tight spaces. */
  compact?: boolean
  /** Prefix non-negative values with "+", matching the app's existing delta convention. */
  signed?: boolean
  /** BCP-47 locale for separators/symbol placement. Defaults like `formatCurrency` does. */
  locale?: string
}

/**
 * A formatted amount carries no meaning if a rounded-to-zero negative value
 * still shows a bare minus sign ("-$0"), which tiny residues and compact
 * notation both produce. Strip the sign when nothing but zeros survived.
 */
function stripNegativeZero(formatted: string, value: number): string {
  if (value >= 0) return formatted
  if (/[1-9]/.test(formatted)) return formatted
  return formatted.replace(/^[-−]/, '')
}

/**
 * Format a money value for a chart: axis tick, tooltip row or legend.
 *
 * @param value    The amount. `null`/`undefined` renders as "—", same as `formatCurrency`.
 * @param currency ISO 4217 currency code.
 * @param options  `compact` for "$1.2K" style display; `signed` to prefix "+" on >= 0.
 */
export function formatMoney(
  value: number | null | undefined,
  currency = 'USD',
  options: FormatMoneyOptions = {},
): string {
  if (value == null) return '—'
  const { compact = false, signed = false, locale = 'en-US' } = options
  const prefix = signed && value >= 0 ? '+' : ''

  if (!compact) {
    return `${prefix}${formatCurrency(value, currency, locale)}`
  }

  try {
    const formatted = new Intl.NumberFormat(locale, {
      style: 'currency',
      currency: currency || 'USD',
      notation: 'compact',
      maximumFractionDigits: 1,
    }).format(value)
    return `${prefix}${stripNegativeZero(formatted, value)}`
  } catch {
    const formatted = new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      notation: 'compact',
      maximumFractionDigits: 1,
    }).format(value)
    return `${prefix}${stripNegativeZero(formatted, value)}`
  }
}

/**
 * Format a 0–1 ratio as a percentage, e.g. `formatPct(0.164)` -> "16.4%".
 *
 * @param fraction The ratio (0.5, not 50).
 * @param digits   Fraction digits to show. Defaults to 1, matching the rest of the app.
 */
export function formatPct(fraction: number | null | undefined, digits = 1): string {
  if (fraction == null || !Number.isFinite(fraction)) return '—'
  return new Intl.NumberFormat('en-US', {
    style: 'percent',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(fraction)
}

/**
 * Localized "month year" label for an ISO date, e.g. "2026-10-01" -> "Oct 2026".
 * Short form: chart axes and legends have far less room than the dashboard's
 * full-word `monthLabel` (lib/month-utils.ts).
 */
export function formatMonth(isoDate: string, locale = 'en-US'): string {
  const date = new Date(`${isoDate.slice(0, 10)}T00:00:00`)
  if (Number.isNaN(date.getTime())) return isoDate
  try {
    return date.toLocaleDateString(locale, { month: 'short', year: 'numeric' })
  } catch {
    return date.toLocaleDateString('en-US', { month: 'short', year: 'numeric' })
  }
}

/**
 * "Qn YYYY" label for an ISO date, e.g. "2026-10-01" -> "Q4 2026". Quarters
 * carry no language-specific wording, so there is no locale parameter.
 */
export function formatQuarter(isoDate: string): string {
  const [yearStr, monthStr] = isoDate.slice(0, 7).split('-')
  const year = Number(yearStr)
  const month = Number(monthStr)
  if (!Number.isFinite(year) || !Number.isFinite(month)) return isoDate
  const quarter = Math.floor((month - 1) / 3) + 1
  return `Q${quarter} ${year}`
}
