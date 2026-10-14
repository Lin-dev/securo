/**
 * Resolves the `--chart-*` CSS custom properties (src/index.css) into
 * concrete colour strings for recharts props, which take real colours (or
 * `var(--chart-1)` strings recharts does not reliably resolve inside SVG
 * `fill`/`stroke` in every browser) rather than CSS custom properties.
 *
 * Kept separate from the `--color-chart-*` Tailwind theme because recharts
 * needs plain strings handed to component props, not utility classes.
 */

export interface ChartTheme {
  /** chart-1 .. chart-8, the Okabe-Ito colour-blind-safe series palette. */
  series: string[]
  grid: string
  axis: string
  positive: string
  negative: string
}

const SERIES_VAR_NAMES = [
  '--chart-1',
  '--chart-2',
  '--chart-3',
  '--chart-4',
  '--chart-5',
  '--chart-6',
  '--chart-7',
  '--chart-8',
] as const

// Mirrors src/index.css :root (light) and .dark. Used whenever a value can't
// be read from computed styles — tests run with `css: false` (vitest.config.ts),
// so index.css is never loaded there, and the kit still needs to render real
// colours rather than empty strings.
const LIGHT_DEFAULTS: ChartTheme = {
  series: ['#E69F00', '#56B4E9', '#009E73', '#F0E442', '#0072B2', '#D55E00', '#CC79A7', '#767676'],
  grid: '#E8ECF1',
  axis: '#64748B',
  positive: '#059669',
  negative: '#F43F5E',
}

const DARK_DEFAULTS: ChartTheme = {
  series: ['#F2C55F', '#74BDE7', '#5CEBC4', '#F3EC91', '#4CB5F0', '#F19A55', '#D289B1', '#9E9E9E'],
  grid: '#2A2D3A',
  axis: '#8A8F9E',
  positive: '#34D399',
  negative: '#FB7185',
}

/**
 * A minimal shape of what `getComputedStyle` returns — just enough to read
 * custom properties, and easy to pass a plain object for in a unit test.
 */
export interface ComputedStyleLike {
  getPropertyValue(name: string): string
}

/**
 * Resolve a `ChartTheme` from computed styles, falling back per-token to the
 * matching default (light or dark) when a custom property is unset or blank.
 *
 * @param styles Typically `getComputedStyle(document.documentElement)`. Pass
 *   `null` (e.g. in a non-DOM test) to get the defaults outright.
 * @param isDark Which default set to fall back to.
 */
export function resolveChartTheme(styles: ComputedStyleLike | null, isDark: boolean): ChartTheme {
  const defaults = isDark ? DARK_DEFAULTS : LIGHT_DEFAULTS

  const read = (varName: string, fallback: string): string => {
    const value = styles?.getPropertyValue(varName)?.trim()
    return value || fallback
  }

  return {
    series: SERIES_VAR_NAMES.map((name, i) => read(name, defaults.series[i])),
    grid: read('--chart-grid', defaults.grid),
    axis: read('--chart-axis', defaults.axis),
    positive: read('--chart-positive', defaults.positive),
    negative: read('--chart-negative', defaults.negative),
  }
}
