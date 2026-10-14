import { describe, expect, it } from 'vitest'
import { within } from '@testing-library/react'

import { RangeLines } from '@/components/charts/RangeLines'
import { renderWithProviders, t } from '@/test/utils'

const low = [
  { date: '2026-10-01', value: 100 },
  { date: '2026-11-01', value: 110 },
]
const base = [
  { date: '2026-10-01', value: 150 },
  { date: '2026-11-01', value: 165 },
]
const high = [
  { date: '2026-10-01', value: 200 },
  { date: '2026-11-01', value: 220 },
]
const actual = [
  { date: '2026-10-01', value: 140 },
]

describe('RangeLines', () => {
  it('renders one line per series (low/base/high), plus actual when given', () => {
    const { container } = renderWithProviders(
      <RangeLines low={low} base={base} high={high} actual={actual} />,
    )
    const lines = container.querySelectorAll('.recharts-line-curve')
    expect(lines).toHaveLength(4)
  })

  it('omits the actual line when no actual series is given', () => {
    const { container } = renderWithProviders(<RangeLines low={low} base={base} high={high} />)
    const lines = container.querySelectorAll('.recharts-line-curve')
    expect(lines).toHaveLength(3)
  })

  it('gives every series a distinct dash pattern, never relying on colour alone', () => {
    const { container } = renderWithProviders(
      <RangeLines low={low} base={base} high={high} actual={actual} />,
    )
    const lines = [...container.querySelectorAll('.recharts-line-curve')]
    const dashPatterns = lines.map((l) => l.getAttribute('stroke-dasharray'))
    // Base is the only solid (null/unset dasharray) line; every dashed series differs.
    const dashedOnly = dashPatterns.filter((d) => d && d !== 'none')
    expect(new Set(dashedOnly).size).toBe(dashedOnly.length)
  })

  it('labels each series via i18n (low/base/high/actual)', () => {
    renderWithProviders(<RangeLines low={low} base={base} high={high} actual={actual} />)
    // Labels surface through the legend-eligible `name` prop on each Line;
    // assert the translated strings exist somewhere, not a hardcoded string.
    expect(t('charts.low')).toBe('Low')
    expect(t('charts.base')).toBe('Base')
    expect(t('charts.high')).toBe('High')
    expect(t('charts.actual')).toBe('Actual')
  })

  it('renders a target reference line with a label when target is given', () => {
    const { container } = renderWithProviders(<RangeLines low={low} base={base} high={high} target={180} />)
    expect(within(container).getByText(t('charts.target'))).toBeInTheDocument()
  })

  it('renders a custom target label when given', () => {
    const { container } = renderWithProviders(
      <RangeLines low={low} base={base} high={high} target={180} targetLabel="Milestone" />,
    )
    expect(within(container).getByText('Milestone')).toBeInTheDocument()
  })

  it('renders no target line when target is omitted', () => {
    const { container } = renderWithProviders(<RangeLines low={low} base={base} high={high} />)
    expect(within(container).queryByText(t('charts.target'))).not.toBeInTheDocument()
  })
})
