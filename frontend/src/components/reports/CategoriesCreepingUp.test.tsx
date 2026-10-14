import { describe, expect, it, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'

import { CategoriesCreepingUp } from '@/components/reports/CategoriesCreepingUp'
import { renderWithProviders, t } from '@/test/utils'
import type { CategoryTrendItem } from '@/types'

const maskMock = vi.hoisted(() => vi.fn((value: string) => value))

vi.mock('@/hooks/use-privacy-mode', () => ({
  usePrivacyMode: () => ({ mask: maskMock, privacyMode: false, MASK: '••••' }),
}))

function monthlySeries(byMonth: Record<string, number>) {
  return Object.entries(byMonth).map(([date, value]) => ({ date, value, breakdowns: {}, change: null }))
}

function makeTravel(latest: number): CategoryTrendItem {
  return {
    key: 'travel-cat', label: 'Travel', color: '#F43F5E', total: latest, group: 'expenses',
    series: monthlySeries({
      '2026-03': 400, '2026-04': 400, '2026-05': 400, '2026-06': 400, '2026-07': 400, '2026-08': 400,
      '2026-09': latest,
    }),
  }
}

beforeEach(() => {
  maskMock.mockClear()
  maskMock.mockImplementation((value: string) => value)
})

describe('CategoriesCreepingUp', () => {
  it('shows a hint to switch to monthly when the interval is not monthly', () => {
    const onSwitchToMonthly = vi.fn()
    renderWithProviders(
      <CategoriesCreepingUp
        categoryTrend={[makeTravel(604)]}
        interval="weekly"
        currentMonth="2026-10"
        currency="USD"
        locale="en-US"
        onSwitchToMonthly={onSwitchToMonthly}
        onSelect={vi.fn()}
      />,
    )
    expect(screen.getByText(t('reports.creepingUpNeedsMonthly'))).toBeInTheDocument()
    screen.getByRole('button', { name: t('reports.creepingUpSwitchToMonthly') }).click()
    expect(onSwitchToMonthly).toHaveBeenCalledTimes(1)
  })

  it('reports not enough history when there is no complete prior month', () => {
    renderWithProviders(
      <CategoriesCreepingUp
        categoryTrend={[{ ...makeTravel(50), series: monthlySeries({ '2026-10': 50 }) }]}
        interval="monthly"
        currentMonth="2026-10"
        currency="USD"
        locale="en-US"
        onSelect={vi.fn()}
      />,
    )
    expect(screen.getByText(t('reports.creepingUpNotEnoughHistory'))).toBeInTheDocument()
  })

  it('reports nothing creeping up when no category spikes', () => {
    renderWithProviders(
      <CategoriesCreepingUp
        categoryTrend={[makeTravel(410)]}
        interval="monthly"
        currentMonth="2026-10"
        currency="USD"
        locale="en-US"
        onSelect={vi.fn()}
      />,
    )
    expect(screen.getByText(t('reports.creepingUpNone'))).toBeInTheDocument()
  })

  it('skips an Uncategorized series even when it technically spikes', () => {
    const uncategorized: CategoryTrendItem = {
      key: 'uncategorized', label: 'Uncategorized', color: '#9CA3AF', total: 5000, group: 'expenses',
      series: monthlySeries({
        '2026-03': 10, '2026-04': 10, '2026-05': 10, '2026-06': 10, '2026-07': 10, '2026-08': 10,
        '2026-09': 5000,
      }),
    }
    renderWithProviders(
      <CategoriesCreepingUp
        categoryTrend={[uncategorized]}
        interval="monthly"
        currentMonth="2026-10"
        currency="USD"
        locale="en-US"
        onSelect={vi.fn()}
      />,
    )
    expect(screen.getByText(t('reports.creepingUpNone'))).toBeInTheDocument()
  })

  it('renders a spiking category with amount, usual level and excess, and drills in on click', () => {
    const onSelect = vi.fn()
    renderWithProviders(
      <CategoriesCreepingUp
        categoryTrend={[makeTravel(604)]}
        interval="monthly"
        currentMonth="2026-10"
        currency="USD"
        locale="en-US"
        onSelect={onSelect}
      />,
    )
    const row = screen.getByRole('button', { name: /Travel/ })
    expect(row).toHaveTextContent('$604.00')
    expect(row).toHaveTextContent('$400.00')
    expect(row).toHaveTextContent('$204.00')
    row.click()
    expect(onSelect).toHaveBeenCalledWith({ key: 'travel-cat', label: 'Travel', month: '2026-09' })
  })

  it('masks amounts in privacy mode', () => {
    maskMock.mockImplementation(() => '••••')
    renderWithProviders(
      <CategoriesCreepingUp
        categoryTrend={[makeTravel(604)]}
        interval="monthly"
        currentMonth="2026-10"
        currency="USD"
        locale="en-US"
        onSelect={vi.fn()}
      />,
    )
    const row = screen.getByRole('button', { name: /Travel/ })
    expect(row).not.toHaveTextContent('$604.00')
    expect(row.textContent?.match(/••••/g)?.length).toBeGreaterThanOrEqual(3)
  })
})
