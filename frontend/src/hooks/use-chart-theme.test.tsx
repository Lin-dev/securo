import { describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'

const themeState = vi.hoisted(() => ({ resolvedTheme: 'light' as string | undefined }))
vi.mock('next-themes', () => ({ useTheme: () => ({ resolvedTheme: themeState.resolvedTheme }) }))

import { useChartTheme } from '@/hooks/use-chart-theme'

describe('useChartTheme', () => {
  it('resolves the light palette by default', () => {
    themeState.resolvedTheme = 'light'
    const { result } = renderHook(() => useChartTheme())

    expect(result.current.series).toHaveLength(8)
    // vitest.config.ts runs with `css: false`, so index.css never loads and
    // getComputedStyle returns no --chart-* values — this must still resolve
    // to the documented light defaults rather than blank strings.
    expect(result.current.series[0]).toBe('#E69F00')
    expect(result.current.positive).toBe('#059669')
    expect(result.current.negative).toBe('#F43F5E')
  })

  it('resolves the dark palette when resolvedTheme is dark', () => {
    themeState.resolvedTheme = 'dark'
    const { result } = renderHook(() => useChartTheme())

    expect(result.current.series[0]).toBe('#F2C55F')
    expect(result.current.positive).toBe('#34D399')
    expect(result.current.negative).toBe('#FB7185')
  })

  it('re-resolves when the theme changes between renders', () => {
    themeState.resolvedTheme = 'light'
    const { result, rerender } = renderHook(() => useChartTheme())
    expect(result.current.series[0]).toBe('#E69F00')

    themeState.resolvedTheme = 'dark'
    rerender()
    expect(result.current.series[0]).toBe('#F2C55F')
  })
})
