import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'

import { SavingsTab } from '@/components/wealth/savings/savings-tab'
import { renderWithProviders, t } from '@/test/utils'
import type { WealthSavingsRateSeries } from '@/types/wealth'

const api = vi.hoisted(() => ({
  wealth: { savingsRate: vi.fn() },
}))
vi.mock('@/lib/api', () => api)

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

const navigate = vi.hoisted(() => vi.fn())
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigate,
}))

function series(overrides: Partial<WealthSavingsRateSeries> = {}): WealthSavingsRateSeries {
  return {
    currency: 'USD',
    months: [
      { month: '2026-08-01', income: 15000, expenses: 11000, direct: 2000, rate: 0.353, cash_rate: 0.267, rolling_3m: null, rolling_12m: null, in_coverage: true, partial: false },
      { month: '2026-09-01', income: 15000, expenses: 12000, direct: 2000, rate: 0.294, cash_rate: 0.2, rolling_3m: null, rolling_12m: null, in_coverage: true, partial: false },
      { month: '2026-10-01', income: 7000, expenses: 6000, direct: 1000, rate: 0.25, cash_rate: 0.143, rolling_3m: 0.31, rolling_12m: null, in_coverage: true, partial: true },
    ],
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  api.wealth.savingsRate.mockResolvedValue(series())
})

afterEach(() => {
  localStorage.clear()
})

describe('SavingsTab', () => {
  it('requests 12 months', async () => {
    renderWithProviders(<SavingsTab />)
    await waitFor(() => expect(api.wealth.savingsRate).toHaveBeenCalledWith(12))
  })

  it('states the 3-month rolling rate as the takeaway title', async () => {
    renderWithProviders(<SavingsTab />)
    expect(await screen.findByText(t('wealthSavings.title.rolling', { rate: '31%' }))).toBeInTheDocument()
  })

  it('draws reference bands at 20%, 35% and 50%', async () => {
    const { container } = renderWithProviders(<SavingsTab />)
    await screen.findByText(t('wealthSavings.title.rolling', { rate: '31%' }))

    const lines = container.querySelectorAll('.recharts-reference-line-line')
    // 0 baseline + 3 bands
    expect(lines.length).toBeGreaterThanOrEqual(4)
    expect(within(container).getAllByText('20%').length).toBeGreaterThan(0)
    expect(within(container).getAllByText('35%').length).toBeGreaterThan(0)
    expect(within(container).getAllByText('50%').length).toBeGreaterThan(0)
  })

  it('marks the partial (current) month, in the chart notes and the table', async () => {
    const { user } = renderWithProviders(<SavingsTab />)
    await screen.findByText(t('wealthSavings.title.rolling', { rate: '31%' }))

    expect(screen.getByText(t('wealthSavings.flags.partial'))).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: t('charts.showTable') }))
    const table = within(screen.getByRole('table'))
    expect(table.getByText(t('wealthSavings.partialBadge'), { exact: false })).toBeInTheDocument()
  })

  it('greys out months outside coverage, with a note and a table badge', async () => {
    const withGap = series()
    withGap.months.push({
      month: '2026-07-01',
      income: 4000,
      expenses: 3000,
      direct: 0,
      rate: null,
      cash_rate: null,
      rolling_3m: null,
      rolling_12m: null,
      in_coverage: false,
      partial: false,
    })
    api.wealth.savingsRate.mockResolvedValue(withGap)

    const { user } = renderWithProviders(<SavingsTab />)
    await screen.findByText(t('wealthSavings.flags.outsideCoverage'))

    await user.click(screen.getByRole('button', { name: t('charts.showTable') }))
    const table = within(screen.getByRole('table'))
    expect(table.getByText(t('wealthSavings.outsideCoverageBadge'), { exact: false })).toBeInTheDocument()
  })

  it('switches bars to the cash rate when the toggle is on', async () => {
    const { user } = renderWithProviders(<SavingsTab />)
    await screen.findByText(t('wealthSavings.title.rolling', { rate: '31%' }))

    expect(screen.getAllByText(t('wealthSavings.rateLabel')).length).toBeGreaterThan(0)

    await user.click(screen.getByRole('switch', { name: t('wealthSavings.cashToggleLabel') }))

    expect(screen.getAllByText(t('wealthSavings.cashRateLabel')).length).toBeGreaterThan(0)
    expect(screen.queryByText(t('wealthSavings.rateLabel'))).not.toBeInTheDocument()
  })

  it("clicking a month's table link navigates to that month's Transactions window", async () => {
    const { user } = renderWithProviders(<SavingsTab />)
    await screen.findByText(t('wealthSavings.title.rolling', { rate: '31%' }))

    await user.click(screen.getByRole('button', { name: t('charts.showTable') }))
    const table = within(screen.getByRole('table'))
    const link = table.getByRole('link', { name: /Sep 2026/ })
    expect(link).toHaveAttribute('href', '/transactions?from=2026-09-01&to=2026-09-30')

    await user.click(link)
  })

  it('clicking a bar navigates to that month\'s Transactions window', async () => {
    const { container, user } = renderWithProviders(<SavingsTab />)
    await screen.findByText(t('wealthSavings.title.rolling', { rate: '31%' }))

    const bars = container.querySelectorAll('.recharts-bar-rectangle')
    expect(bars.length).toBe(3)
    await user.click(bars[0])

    expect(navigate).toHaveBeenCalledWith('/transactions?from=2026-08-01&to=2026-08-31')
  })

  it('lists every month in the table fallback', async () => {
    const { user } = renderWithProviders(<SavingsTab />)
    await screen.findByText(t('wealthSavings.title.rolling', { rate: '31%' }))

    await user.click(screen.getByRole('button', { name: t('charts.showTable') }))
    const table = within(screen.getByRole('table'))
    expect(table.getByText('Aug 2026', { exact: false })).toBeInTheDocument()
    expect(table.getByText('Sep 2026', { exact: false })).toBeInTheDocument()
    expect(table.getByText('Oct 2026', { exact: false })).toBeInTheDocument()
  })

  it('masks income, expenses and rate values in privacy mode', async () => {
    localStorage.setItem('privacyMode', 'true')
    const { user } = renderWithProviders(<SavingsTab />)
    await screen.findByText(t('wealthSavings.title.rolling', { rate: '•••••' }))

    await user.click(screen.getByRole('button', { name: t('charts.showTable') }))
    const table = within(screen.getByRole('table'))
    expect(table.queryByText('$15,000.00')).not.toBeInTheDocument()
    expect(table.getAllByText('•••••').length).toBeGreaterThan(0)
  })
})
