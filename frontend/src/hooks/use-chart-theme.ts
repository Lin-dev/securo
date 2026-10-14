import { useMemo } from 'react'
import { useTheme } from 'next-themes'

import { resolveChartTheme, type ChartTheme } from '@/lib/chart-theme'

/**
 * Resolve the `--chart-*` tokens to concrete colours for recharts props.
 *
 * Re-reads `getComputedStyle` whenever `resolvedTheme` changes, so a chart
 * mounted before a light/dark toggle still picks up the new palette — the
 * CSS custom properties flip instantly via the `.dark` class (next-themes,
 * see components/theme-provider.tsx), but nothing re-renders a chart that
 * already captured colour strings unless something reacts to the toggle.
 */
export function useChartTheme(): ChartTheme {
  const { resolvedTheme } = useTheme()
  const isDark = resolvedTheme === 'dark'

  return useMemo(() => {
    const styles = typeof document === 'undefined' ? null : getComputedStyle(document.documentElement)
    return resolveChartTheme(styles, isDark)
  }, [isDark])
}
