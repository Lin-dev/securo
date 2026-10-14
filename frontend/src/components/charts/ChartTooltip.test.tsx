import { afterEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import { ChartTooltip } from '@/components/charts/ChartTooltip'

afterEach(() => {
  localStorage.clear()
})

describe('ChartTooltip', () => {
  it('renders nothing when inactive', () => {
    const { container } = render(
      <ChartTooltip active={false} payload={[{ name: 'Base', value: 100, dataKey: 'base' }]} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing with an empty payload', () => {
    const { container } = render(<ChartTooltip active payload={[]} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('formats a money row with the given currency', () => {
    render(
      <ChartTooltip
        active
        currency="USD"
        payload={[{ name: 'Base', value: 1234, dataKey: 'base', color: '#0072B2' }]}
      />,
    )
    expect(screen.getByText('$1,234.00')).toBeInTheDocument()
    expect(screen.getByText('Base')).toBeInTheDocument()
  })

  it('formats a percent row when valueType is percent', () => {
    render(
      <ChartTooltip active valueType="percent" payload={[{ name: 'Savings rate', value: 0.164, dataKey: 'rate' }]} />,
    )
    expect(screen.getByText('16.4%')).toBeInTheDocument()
  })

  it('applies a per-series type override', () => {
    render(
      <ChartTooltip
        active
        currency="USD"
        valueType="money"
        seriesTypes={{ rate: 'percent' }}
        payload={[
          { name: 'Balance', value: 500, dataKey: 'balance' },
          { name: 'Rate', value: 0.5, dataKey: 'rate' },
        ]}
      />,
    )
    expect(screen.getByText('$500.00')).toBeInTheDocument()
    expect(screen.getByText('50.0%')).toBeInTheDocument()
  })

  it('renders a label through labelFormatter', () => {
    render(
      <ChartTooltip
        active
        label="2026-10"
        labelFormatter={(l) => `Month: ${l}`}
        payload={[{ name: 'Base', value: 1, dataKey: 'base' }]}
      />,
    )
    expect(screen.getByText('Month: 2026-10')).toBeInTheDocument()
  })

  it('masks every value while privacy mode is on', () => {
    localStorage.setItem('privacyMode', 'true')

    render(
      <ChartTooltip
        active
        currency="USD"
        payload={[{ name: 'Base', value: 1234, dataKey: 'base' }]}
      />,
    )

    expect(screen.queryByText('$1,234.00')).not.toBeInTheDocument()
    expect(screen.getByText('•••••')).toBeInTheDocument()
  })
})
