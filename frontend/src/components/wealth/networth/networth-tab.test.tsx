import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import { useLocation } from 'react-router-dom'

import { NetWorthTab } from '@/components/wealth/networth/networth-tab'
import { renderWithProviders, t } from '@/test/utils'
import type { WealthBucket, WealthNetWorthHistory } from '@/types/wealth'

const api = vi.hoisted(() => ({
  wealth: { netWorthHistory: vi.fn() },
}))
vi.mock('@/lib/api', () => api)

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

function buckets(over: Partial<Record<WealthBucket, number>> = {}): Record<WealthBucket, number> {
  return {
    cash: 0,
    taxable: 0,
    retirement_pretax: 0,
    retirement_roth: 0,
    retirement_hsa: 0,
    crypto: 0,
    property_other: 0,
    card_debt: 0,
    other_debt: 0,
    ...over,
  }
}

function history(overrides: Partial<WealthNetWorthHistory> = {}): WealthNetWorthHistory {
  return {
    currency: 'USD',
    synced_from: '2026-09-30',
    points: [
      {
        date: '2026-08-31',
        total: 480000,
        liquid: 150000,
        buckets: buckets({ cash: 50000, taxable: 100000, card_debt: -4000 }),
        flags: ['estimated'],
      },
      {
        date: '2026-09-30',
        total: 490000,
        liquid: 195000,
        buckets: buckets({ cash: 60000, taxable: 140000, crypto: 5000, card_debt: -5000 }),
        flags: [],
      },
      {
        date: '2026-10-14',
        total: 500000,
        liquid: 200000,
        buckets: buckets({ cash: 62000, taxable: 143000, crypto: 5000, card_debt: -5000 }),
        flags: [],
      },
    ],
    ...overrides,
  }
}

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="location-search">{location.search}</div>
}

beforeEach(() => {
  vi.clearAllMocks()
  api.wealth.netWorthHistory.mockResolvedValue(history())
})

afterEach(() => {
  localStorage.clear()
})

describe('NetWorthTab', () => {
  it('requests 12 months by default', async () => {
    renderWithProviders(<NetWorthTab />)
    await waitFor(() => expect(api.wealth.netWorthHistory).toHaveBeenCalledWith(12))
  })

  it('defaults to the synced period: the table omits points before synced_from', async () => {
    const { user } = renderWithProviders(<NetWorthTab />)
    await user.click(await screen.findByRole('button', { name: t('charts.showTable') }))

    const table = within(screen.getByRole('table'))
    expect(table.queryByText('Aug 2026')).not.toBeInTheDocument()
    expect(table.getByText('Sep 2026')).toBeInTheDocument()
    expect(table.getByText('Oct 2026')).toBeInTheDocument()
  })

  it('shows the pre-sync point once "Include estimated history" is on', async () => {
    const { user } = renderWithProviders(<NetWorthTab />)

    await user.click(await screen.findByRole('switch', { name: t('wealthNetworth.estimatedToggleLabel') }))
    await user.click(await screen.findByRole('button', { name: t('charts.showTable') }))

    expect(within(screen.getByRole('table')).getByText('Aug 2026')).toBeInTheDocument()
  })

  it('shows every point with the estimated note when synced_from is null, with no toggle', async () => {
    api.wealth.netWorthHistory.mockResolvedValue(history({ synced_from: null }))
    const { user } = renderWithProviders(<NetWorthTab />)

    expect(screen.queryByRole('switch', { name: t('wealthNetworth.estimatedToggleLabel') })).not.toBeInTheDocument()
    expect(await screen.findByText(t('wealthNetworth.flags.estimatedAll'))).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: t('charts.showTable') }))
    expect(within(screen.getByRole('table')).getByText('Aug 2026')).toBeInTheDocument()
  })

  it('?liquid=1 switches to the liquid series and keeps the other params', async () => {
    const { user } = renderWithProviders(
      <>
        <NetWorthTab />
        <LocationProbe />
      </>,
      { route: '/wealth?tab=networth' },
    )

    await user.click(screen.getByRole('switch', { name: t('wealthNetworth.liquidToggleLabel') }))

    expect(screen.getByTestId('location-search').textContent).toBe('?tab=networth&liquid=1')
    expect(screen.getAllByText(t('wealthNetworth.liquidLabel'), { exact: false }).length).toBeGreaterThan(0)
  })

  it('changing the months selector changes the request', async () => {
    const { user } = renderWithProviders(<NetWorthTab />)
    await waitFor(() => expect(api.wealth.netWorthHistory).toHaveBeenCalledWith(12))

    await user.click(screen.getByRole('combobox', { name: t('wealthNetworth.monthsLabel') }))
    await user.click(await screen.findByRole('option', { name: t('wealthNetworth.monthsOption', { count: 24 }) }))

    await waitFor(() => expect(api.wealth.netWorthHistory).toHaveBeenCalledWith(24))
  })

  it('shows no flag footnotes when no point carries one', async () => {
    renderWithProviders(<NetWorthTab />)
    await waitFor(() => expect(api.wealth.netWorthHistory).toHaveBeenCalled())

    // The estimated note only appears once the toggle brings estimated
    // points into view (or synced_from is null) -- neither applies here.
    expect(screen.queryByText(t('wealthNetworth.flags.estimatedAll'), { exact: false })).not.toBeInTheDocument()
    expect(screen.queryByText(t('wealthNetworth.flags.cashFlat'))).not.toBeInTheDocument()
    expect(screen.queryByText(t('wealthNetworth.flags.noHoldings'))).not.toBeInTheDocument()
  })

  it('shows the cash_flat footnote only when a visible point carries it', async () => {
    const withFlag = history()
    withFlag.points[2] = { ...withFlag.points[2], flags: ['cash_flat'] }
    api.wealth.netWorthHistory.mockResolvedValue(withFlag)

    renderWithProviders(<NetWorthTab />)
    expect(await screen.findByText(t('wealthNetworth.flags.cashFlat'))).toBeInTheDocument()
    expect(screen.queryByText(t('wealthNetworth.flags.noHoldings'))).not.toBeInTheDocument()
  })

  it('shows the no_holdings footnote only when a visible point carries it', async () => {
    const withFlag = history()
    withFlag.points[2] = { ...withFlag.points[2], flags: ['no_holdings'] }
    api.wealth.netWorthHistory.mockResolvedValue(withFlag)

    renderWithProviders(<NetWorthTab />)
    expect(await screen.findByText(t('wealthNetworth.flags.noHoldings'))).toBeInTheDocument()
    expect(screen.queryByText(t('wealthNetworth.flags.cashFlat'))).not.toBeInTheDocument()
  })

  it('masks table values in privacy mode', async () => {
    localStorage.setItem('privacyMode', 'true')
    const { user } = renderWithProviders(<NetWorthTab />)

    await user.click(await screen.findByRole('button', { name: t('charts.showTable') }))

    const table = screen.getByRole('table')
    expect(within(table).queryByText('$490,000.00')).not.toBeInTheDocument()
    expect(within(table).getAllByText('•••••').length).toBeGreaterThan(0)
  })

  it('shows unmasked table values with privacy mode off', async () => {
    const { user } = renderWithProviders(<NetWorthTab />)
    await user.click(await screen.findByRole('button', { name: t('charts.showTable') }))

    const table = screen.getByRole('table')
    expect(within(table).getByText('$490,000.00')).toBeInTheDocument()
  })
})
