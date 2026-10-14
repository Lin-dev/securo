import { describe, expect, it } from 'vitest'
import { screen } from '@testing-library/react'

import { ChartCard } from '@/components/charts/ChartCard'
import { renderWithProviders, t } from '@/test/utils'

describe('ChartCard', () => {
  it('renders the takeaway title and the chart children', () => {
    renderWithProviders(
      <ChartCard title="Spending is up 12% this month">
        <div data-testid="chart">chart</div>
      </ChartCard>,
    )

    expect(screen.getByText('Spending is up 12% this month')).toBeInTheDocument()
    expect(screen.getByTestId('chart')).toBeInTheDocument()
  })

  it('renders an optional subtitle and footnote', () => {
    renderWithProviders(
      <ChartCard title="Takeaway" subtitle="A subtitle" footnote="Based on 94 days of data">
        <div>chart</div>
      </ChartCard>,
    )

    expect(screen.getByText('A subtitle')).toBeInTheDocument()
    expect(screen.getByText('Based on 94 days of data')).toBeInTheDocument()
  })

  it('shows a loading state instead of the chart, with no table toggle', () => {
    renderWithProviders(
      <ChartCard title="Takeaway" loading table={{ columns: [{ key: 'a', label: 'A' }], rows: [] }}>
        <div data-testid="chart">chart</div>
      </ChartCard>,
    )

    expect(screen.queryByTestId('chart')).not.toBeInTheDocument()
    expect(screen.queryByText(t('charts.showTable'))).not.toBeInTheDocument()
  })

  it('shows the empty state instead of the chart', () => {
    renderWithProviders(
      <ChartCard title="Takeaway" empty>
        <div data-testid="chart">chart</div>
      </ChartCard>,
    )

    expect(screen.getByText(t('charts.empty'))).toBeInTheDocument()
    expect(screen.queryByTestId('chart')).not.toBeInTheDocument()
  })

  it('has no table toggle when no table prop is given', () => {
    renderWithProviders(
      <ChartCard title="Takeaway">
        <div>chart</div>
      </ChartCard>,
    )

    expect(screen.queryByText(t('charts.showTable'))).not.toBeInTheDocument()
  })

  it('toggles between the chart and the table fallback, with an accessible label', async () => {
    const { user } = renderWithProviders(
      <ChartCard
        title="Takeaway"
        table={{
          columns: [{ key: 'month', label: 'Month' }, { key: 'value', label: 'Value' }],
          rows: [{ month: 'Oct', value: '$100' }],
        }}
      >
        <div data-testid="chart">chart</div>
      </ChartCard>,
    )

    expect(screen.getByTestId('chart')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()

    const toggle = screen.getByRole('button', { name: t('charts.showTable') })
    expect(toggle).toHaveAttribute('aria-pressed', 'false')

    await user.click(toggle)

    expect(screen.queryByTestId('chart')).not.toBeInTheDocument()
    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(screen.getByText('Month')).toBeInTheDocument()
    expect(screen.getByText('$100')).toBeInTheDocument()

    const toggleAfter = screen.getByRole('button', { name: t('charts.hideTable') })
    expect(toggleAfter).toHaveAttribute('aria-pressed', 'true')

    await user.click(toggleAfter)

    expect(screen.getByTestId('chart')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })
})
