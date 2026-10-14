/**
 * Day-level date formatting for the Cash tab (plan B5b/S8).
 *
 * The shared chart kit's `formatMonth`/`formatQuarter` (lib/chart-format.ts)
 * only have month granularity, which would collapse every point of a
 * 30/90-day window onto the same label. `lib/chart-format.ts` is outside
 * this slice's files, so this stays local rather than growing the shared kit.
 */
export function formatCashDay(isoDate: string, locale = 'en-US'): string {
  const date = new Date(`${isoDate.slice(0, 10)}T00:00:00`)
  if (Number.isNaN(date.getTime())) return isoDate
  try {
    return date.toLocaleDateString(locale, { month: 'short', day: 'numeric' })
  } catch {
    return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  }
}
