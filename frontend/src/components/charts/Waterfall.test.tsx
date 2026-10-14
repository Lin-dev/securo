import { afterEach, describe, expect, it } from 'vitest'
import { within } from '@testing-library/react'

import { Waterfall } from '@/components/charts/Waterfall'
import { renderWithProviders, t } from '@/test/utils'

afterEach(() => {
  localStorage.clear()
})

const steps = [
  { label: 'Income', value: 5000 },
  { label: 'Housing', value: -1800 },
  { label: 'Food', value: -600 },
]

describe('Waterfall', () => {
  it('renders one bar per step plus an appended total', () => {
    const { container } = renderWithProviders(<Waterfall steps={steps} currency="USD" />)
    // One rect per "delta" bar (step + total); the invisible "base" bars are transparent.
    const bars = container.querySelectorAll('.recharts-bar-rectangle')
    expect(bars.length).toBeGreaterThanOrEqual(steps.length + 1)
  })

  it('labels the total bar with the translated "Total" by default', () => {
    const { container } = renderWithProviders(<Waterfall steps={steps} currency="USD" />)
    expect(within(container).getByText(t('charts.total'))).toBeInTheDocument()
  })

  it('accepts a custom total label', () => {
    const { container } = renderWithProviders(<Waterfall steps={steps} currency="USD" totalLabel="Net" />)
    expect(within(container).getByText('Net')).toBeInTheDocument()
    expect(within(container).queryByText(t('charts.total'))).not.toBeInTheDocument()
  })

  it('shows every step label on the axis', () => {
    const { container } = renderWithProviders(<Waterfall steps={steps} currency="USD" />)
    const scoped = within(container)
    expect(scoped.getByText('Income')).toBeInTheDocument()
    expect(scoped.getByText('Housing')).toBeInTheDocument()
    expect(scoped.getByText('Food')).toBeInTheDocument()
  })

  it("carries a sign glyph in each bar's value label, masked in privacy mode", () => {
    const { container } = renderWithProviders(<Waterfall steps={steps} currency="USD" />)
    const scoped = within(container)
    expect(scoped.getByText('+$5,000.00')).toBeInTheDocument()
    expect(scoped.getByText('-$1,800.00')).toBeInTheDocument()
    expect(scoped.getByText('-$600.00')).toBeInTheDocument()
    // Running total: 5000 - 1800 - 600 = 2600
    expect(scoped.getByText('+$2,600.00')).toBeInTheDocument()
  })

  it('masks every bar value label in privacy mode', () => {
    localStorage.setItem('privacyMode', 'true')
    const { container } = renderWithProviders(<Waterfall steps={steps} currency="USD" />)
    const scoped = within(container)

    expect(scoped.queryByText('+$5,000.00')).not.toBeInTheDocument()
    expect(scoped.getAllByText('•••••').length).toBeGreaterThan(0)
  })

  it('gives negative and positive steps different colours', () => {
    const { container } = renderWithProviders(<Waterfall steps={steps} currency="USD" />)
    const cells = [...container.querySelectorAll('.recharts-bar-rectangle path, .recharts-bar-rectangle rect')]
    const fills = new Set(cells.map((c) => c.getAttribute('fill')).filter((f) => f && f !== 'transparent'))
    expect(fills.size).toBeGreaterThan(1)
  })
})
