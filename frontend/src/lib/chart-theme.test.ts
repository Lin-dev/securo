import { describe, expect, it } from 'vitest'

import { resolveChartTheme } from '@/lib/chart-theme'

describe('resolveChartTheme', () => {
  it('returns the light defaults when styles is null and isDark is false', () => {
    const theme = resolveChartTheme(null, false)
    expect(theme.series).toHaveLength(8)
    expect(theme.series[0]).toBe('#E69F00')
    expect(theme.positive).toBe('#059669')
    expect(theme.negative).toBe('#F43F5E')
  })

  it('returns the dark defaults when styles is null and isDark is true', () => {
    const theme = resolveChartTheme(null, true)
    expect(theme.series[0]).toBe('#F2C55F')
    expect(theme.positive).toBe('#34D399')
    expect(theme.negative).toBe('#FB7185')
  })

  it('prefers a value read from computed styles over the default', () => {
    const styles = { getPropertyValue: (name: string) => (name === '--chart-1' ? '#123456' : '') }
    const theme = resolveChartTheme(styles, false)
    expect(theme.series[0]).toBe('#123456')
    // Untouched tokens still fall back.
    expect(theme.series[1]).toBe('#56B4E9')
  })

  it('falls back when a computed style value is blank whitespace', () => {
    const styles = { getPropertyValue: () => '   ' }
    const theme = resolveChartTheme(styles, false)
    expect(theme.grid).toBe('#E8ECF1')
  })

  it('returns 8 distinct series colours in each mode', () => {
    for (const isDark of [false, true]) {
      const { series } = resolveChartTheme(null, isDark)
      expect(new Set(series).size).toBe(8)
    }
  })
})
