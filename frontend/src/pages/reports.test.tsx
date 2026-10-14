import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { useSearchParams } from 'react-router-dom'

import ReportsPage from '@/pages/reports'
import { renderWithProviders, t } from '@/test/utils'
import { previousWindow } from '@/lib/chart-period'
import type { ReportResponse } from '@/types'

const api = vi.hoisted(() => ({
  reports: {
    netWorth: vi.fn(),
    incomeExpenses: vi.fn(),
    cashFlow: vi.fn(),
  },
  transactions: { list: vi.fn() },
  dashboard: { projectedTransactions: vi.fn() },
  admin: { accountingMode: vi.fn() },
}))

const navigateMock = vi.hoisted(() => vi.fn())

vi.mock('@/lib/api', () => ({
  reports: api.reports,
  transactions: api.transactions,
  dashboard: api.dashboard,
  admin: api.admin,
}))

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => navigateMock }
})

/** Exposes the current `?tab=` (and the rest of the query string) so tests
 * can assert on what the page WROTE to the URL, not just what it reads. */
function SearchParamsProbe() {
  const [params] = useSearchParams()
  return <div data-testid="search-params">{params.toString()}</div>
}

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }),
}))

vi.mock('@/contexts/collection-filter-context', () => ({
  useCollectionFilter: () => ({ activeAccountIds: null, activeWalletIds: null }),
}))

vi.mock('@/hooks/use-privacy-mode', () => ({
  usePrivacyMode: () => ({ mask: (value: string) => value, privacyMode: false, MASK: '••••' }),
}))

vi.mock('@/hooks/use-timezone', () => ({
  useEffectiveTimezone: () => undefined,
}))

function emptyReport(type: string): ReportResponse {
  return {
    summary: { primary_value: 1000, change_amount: 100, change_percent: 5, breakdowns: [] },
    trend: [{ date: '2026-01-01', value: 1000, breakdowns: {}, change: null }],
    meta: { type, series_keys: [], currency: 'USD', interval: 'monthly' },
    composition: [],
    category_trend: [],
  }
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(2026, 8, 12, 12))
  vi.clearAllMocks()
  api.reports.netWorth.mockResolvedValue(emptyReport('net_worth'))
  api.reports.incomeExpenses.mockResolvedValue(emptyReport('income_expenses'))
  api.reports.cashFlow.mockResolvedValue(emptyReport('cash_flow'))
  api.transactions.list.mockResolvedValue({ items: [] })
  api.dashboard.projectedTransactions.mockResolvedValue([])
  api.admin.accountingMode.mockResolvedValue({ mode: 'cash' })
})

afterEach(() => vi.useRealTimers())

describe('Reports page — Custom range segment', () => {
  it('loads the Net Worth tab with the 1Y preset by default', async () => {
    renderWithProviders(<ReportsPage />)

    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalled())
    const [, , , , , startDate, endDate] = api.reports.netWorth.mock.calls[0]
    expect(startDate).toBeUndefined()
    expect(endDate).toBeUndefined()
  })

  it('opening Custom waits for Apply before querying January 1 through today', async () => {
    const { user } = renderWithProviders(<ReportsPage />)
    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalledTimes(1))

    await user.click(screen.getByRole('button', { name: t('reports.customRange') }))

    expect(api.reports.netWorth).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: t('reports.range1y') })).toHaveClass('bg-primary')
    await user.click(screen.getByRole('button', { name: t('transactions.filtersBar.apply') }))
    const year = 2026
    await waitFor(() => {
      const last = api.reports.netWorth.mock.calls.at(-1)!
      expect(last[5]).toBe(`${year}-01-01`)
      expect(last[6]).toBe('2026-09-12')
    })

    // The segment itself now displays the picked range instead of "Custom
    // range" — in the same compact, year-less form as the transactions
    // filter bar's applied-range chip.
    const fmt = (iso: string) =>
      new Date(`${iso}T00:00:00`).toLocaleDateString('en-US', {
        day: '2-digit',
        month: 'short',
      })
    expect(
      screen.getByRole('button', { name: t('reports.customRange') }),
    ).toHaveTextContent(`${fmt(`${year}-01-01`)} - ${fmt('2026-09-12')}`)
  })

  it('is not offered on the Cash Flow tab', async () => {
    const { user } = renderWithProviders(<ReportsPage />)
    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalled())

    await user.click(screen.getByRole('tab', { name: t('reports.cashFlow') }))

    await waitFor(() => expect(api.reports.cashFlow).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: t('reports.customRange') })).not.toBeInTheDocument()
  })
})


it.each(['Cancel', 'Escape', 'outside'] as const)(
  '6M → Custom → %s leaves the preset and query unchanged', async (dismiss) => {
    const { user } = renderWithProviders(<ReportsPage />)
    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalledTimes(1))
    const preset = screen.getByRole('button', { name: t('reports.range6m') })
    await user.click(preset)
    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalledTimes(2))
    await user.click(screen.getByRole('button', { name: t('reports.customRange') }))
    if (dismiss === 'Cancel') {
      await user.click(screen.getByRole('button', { name: t('transactions.filtersBar.cancel') }))
    } else if (dismiss === 'Escape') {
      await user.keyboard('{Escape}')
    } else {
      await user.click(document.body)
    }
    expect(preset).toHaveClass('bg-primary')
    expect(api.reports.netWorth).toHaveBeenCalledTimes(2)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: t('reports.customRange') }))
    expect(api.reports.netWorth).toHaveBeenCalledTimes(2)
    await user.click(screen.getByRole('button', { name: t('transactions.filtersBar.apply') }))
    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalledTimes(3))
  },
)

it.each([
  ['reports.netWorth', 'reports.range1y', 12],
  ['reports.incomeExpenses', 'reports.range1y', 12],
  ['reports.moneyMap', 'reports.range3m', 3],
] as const)('Reset then Apply restores the fallback for %s', async (tab, fallback, months) => {
  const { user } = renderWithProviders(<ReportsPage />)
  if (tab !== 'reports.netWorth') {
    await user.click(screen.getByRole('tab', { name: t(tab) }))
  }
  const custom = screen.getByRole('button', { name: t('reports.customRange') })
  await user.click(custom)
  await user.click(screen.getByRole('button', { name: t('transactions.filtersBar.apply') }))
  const reportApi = tab === 'reports.netWorth' ? api.reports.netWorth : api.reports.incomeExpenses
  await waitFor(() => expect(reportApi.mock.calls.at(-1)?.slice(-2)).toEqual([
    '2026-01-01', '2026-09-12',
  ]))
  await user.click(custom)
  await user.click(screen.getByRole('button', { name: t('transactions.filtersBar.reset') }))
  await user.click(screen.getByRole('button', { name: t('transactions.filtersBar.apply') }))
  await waitFor(() => {
    expect(screen.getByRole('button', { name: t(fallback) })).toHaveClass('bg-primary')
    expect(reportApi.mock.calls.at(-1)?.[0]).toBe(months)
    expect(reportApi.mock.calls.at(-1)?.slice(-2)).toEqual([undefined, undefined])
  })
  expect(custom).toHaveTextContent(t('reports.customRange'))
})

it('preserves saved custom dates across supported tabs and preset dismissal', async () => {
  const { user } = renderWithProviders(<ReportsPage />)
  const custom = screen.getByRole('button', { name: t('reports.customRange') })
  await user.click(custom)
  await user.click(screen.getByRole('button', { name: t('transactions.filtersBar.apply') }))
  for (const tab of ['reports.incomeExpenses', 'reports.moneyMap']) {
    await user.click(screen.getByRole('tab', { name: t(tab) }))
    await waitFor(() => expect(api.reports.incomeExpenses.mock.calls.at(-1)?.slice(-2))
      .toEqual(['2026-01-01', '2026-09-12']))
    expect(custom).toHaveClass('bg-primary')
  }
  await user.click(screen.getByRole('button', { name: t('reports.range6m') }))
  await waitFor(() => expect(api.reports.incomeExpenses.mock.calls.at(-1)?.slice(-2))
    .toEqual([undefined, undefined]))
  const count = api.reports.incomeExpenses.mock.calls.length
  await user.click(custom)
  await user.click(screen.getByRole('button', { name: t('transactions.filtersBar.reset') }))
  await user.click(screen.getByRole('button', { name: t('transactions.filtersBar.cancel') }))
  expect(api.reports.incomeExpenses).toHaveBeenCalledTimes(count)
  expect(screen.getByRole('button', { name: t('reports.range6m') })).toHaveClass('bg-primary')
  await user.click(custom)
  await user.click(screen.getByRole('button', { name: t('transactions.filtersBar.apply') }))
  await waitFor(() => expect(api.reports.incomeExpenses.mock.calls.at(-1)?.slice(-2))
    .toEqual(['2026-01-01', '2026-09-12']))
  await user.click(screen.getByRole('tab', { name: t('reports.cashFlow') }))
  await waitFor(() => expect(api.reports.cashFlow).toHaveBeenCalled())
  expect(screen.queryByRole('button', { name: t('reports.customRange') })).not.toBeInTheDocument()
  expect(api.reports.cashFlow.mock.calls.at(-1)?.[0]).toBe(6)
  await user.click(screen.getByRole('tab', { name: t('reports.netWorth') }))
  await user.click(screen.getByRole('button', { name: t('reports.customRange') }))
  await user.click(screen.getByRole('button', { name: t('transactions.filtersBar.apply') }))
  await waitFor(() => expect(api.reports.netWorth.mock.calls.at(-1)?.slice(-2))
    .toEqual(['2026-01-01', '2026-09-12']))
})

describe('Reports page — query failure', () => {
  it('surfaces a rejected custom range instead of showing stale numbers', async () => {
    const { user } = renderWithProviders(<ReportsPage />)
    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalledTimes(1))

    api.reports.netWorth.mockRejectedValueOnce({
      response: { data: { detail: 'Custom range is too wide (max 10 years)' } },
    })
    await user.click(screen.getByRole('button', { name: t('reports.customRange') }))
    await user.click(screen.getByRole('button', { name: t('transactions.filtersBar.apply') }))

    expect(await screen.findByText('Custom range is too wide (max 10 years)')).toBeInTheDocument()
    // The prior successful report must not linger under the error.
    expect(screen.queryByText(t('reports.trend'), { exact: false })).not.toBeInTheDocument()
    // Tabs and range controls stay usable so the user can correct the request.
    expect(screen.getByRole('button', { name: t('reports.range1y') })).toBeInTheDocument()
  })

  it('shows a generic fallback for a network failure and recovers via Retry', async () => {
    const { user } = renderWithProviders(<ReportsPage />)
    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalledTimes(1))

    api.reports.netWorth.mockRejectedValueOnce(new Error('network error'))
    await user.click(screen.getByRole('button', { name: t('reports.range6m') }))

    expect(await screen.findByText(t('reports.loadError'))).toBeInTheDocument()

    api.reports.netWorth.mockResolvedValueOnce(emptyReport('net_worth'))
    await user.click(screen.getByRole('button', { name: t('common.retry') }))

    await waitFor(() => expect(screen.queryByText(t('reports.loadError'))).not.toBeInTheDocument())
  })

  it('recovers by changing the range after a rejected selection', async () => {
    const { user } = renderWithProviders(<ReportsPage />)
    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalledTimes(1))

    api.reports.netWorth.mockRejectedValueOnce({ response: { data: { detail: 'end_date must be on or before today' } } })
    await user.click(screen.getByRole('button', { name: t('reports.customRange') }))
    await user.click(screen.getByRole('button', { name: t('transactions.filtersBar.apply') }))
    expect(await screen.findByText('end_date must be on or before today')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: t('reports.range6m') }))
    await waitFor(() => expect(screen.queryByText('end_date must be on or before today')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: t('reports.range6m') })).toHaveClass('bg-primary')
  })
})

describe('Reports page — tabs in the URL (plan S6)', () => {
  it('reads the active tab from ?tab= on load', async () => {
    renderWithProviders(<ReportsPage />, { route: '/reports?tab=income_expenses' })
    await waitFor(() => expect(api.reports.incomeExpenses).toHaveBeenCalled())
    expect(screen.getByRole('tab', { name: t('reports.incomeExpenses') })).toHaveAttribute('aria-selected', 'true')
  })

  it('falls back to the first enabled tab for an unknown ?tab=', async () => {
    renderWithProviders(<ReportsPage />, { route: '/reports?tab=bogus' })
    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalled())
    expect(screen.getByRole('tab', { name: t('reports.netWorth') })).toHaveAttribute('aria-selected', 'true')
  })

  it('a reload at a tab URL lands on that same tab (round trip)', async () => {
    renderWithProviders(<ReportsPage />, { route: '/reports?tab=cash_flow' })
    await waitFor(() => expect(api.reports.cashFlow).toHaveBeenCalled())
    expect(screen.getByRole('tab', { name: t('reports.cashFlow') })).toHaveAttribute('aria-selected', 'true')
  })

  it('writes the active tab to ?tab= on selection, with replace, keeping other params', async () => {
    const { user } = renderWithProviders(
      <>
        <ReportsPage />
        <SearchParamsProbe />
      </>,
      { route: '/reports?foo=bar' },
    )
    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalled())
    expect(screen.getByTestId('search-params')).toHaveTextContent('foo=bar')

    await user.click(screen.getByRole('tab', { name: t('reports.incomeExpenses') }))

    await waitFor(() => expect(api.reports.incomeExpenses).toHaveBeenCalled())
    expect(screen.getByTestId('search-params').textContent).toContain('tab=income_expenses')
    expect(screen.getByTestId('search-params').textContent).toContain('foo=bar')
  })
})

describe('Reports page — chart click-through (plan S6)', () => {
  function incomeExpensesWithBars(): ReportResponse {
    return {
      summary: {
        primary_value: 400, change_amount: 400, change_percent: null,
        breakdowns: [
          { key: 'income', label: 'Income', value: 1000, color: '#10B981' },
          { key: 'expenses', label: 'Expenses', value: 600, color: '#F43F5E' },
        ],
      },
      trend: [
        { date: '2026-07', value: 400, breakdowns: { income: 1000, expenses: 600 }, change: null },
      ],
      meta: {
        type: 'income_expenses', series_keys: ['income', 'expenses'], currency: 'USD', interval: 'monthly',
        window_from: '2026-07-01', window_to: '2026-07-31',
      },
      composition: [
        { key: 'groceries', label: 'Groceries', value: 600, color: '#F43F5E', group: 'expenses' },
      ],
      category_trend: [],
    }
  }

  it('an income bar click opens the drill-down over that period window, typed credit, wealth P&L only', async () => {
    api.reports.incomeExpenses.mockResolvedValue(incomeExpensesWithBars())
    const { user } = renderWithProviders(<ReportsPage />, { route: '/reports?tab=income_expenses' })
    await waitFor(() => expect(api.reports.incomeExpenses).toHaveBeenCalled())

    const incomeBar = await screen.findByRole('button', { name: /Income · 2026-07/ })
    await user.click(incomeBar)

    await waitFor(() => expect(api.transactions.list).toHaveBeenCalledWith(expect.objectContaining({
      type: 'credit', from: '2026-07-01', to: '2026-07-31', user_pnl_only: true,
    })))
    const link = await screen.findByRole('link', { name: t('dashboard.viewInTransactions') })
    expect(link.getAttribute('href')).toContain('wealth_pnl_only=1')
    expect(link.getAttribute('href')).toContain('type=credit')
  })

  it('an expense bar click opens the drill-down typed debit', async () => {
    api.reports.incomeExpenses.mockResolvedValue(incomeExpensesWithBars())
    const { user } = renderWithProviders(<ReportsPage />, { route: '/reports?tab=income_expenses' })
    await waitFor(() => expect(api.reports.incomeExpenses).toHaveBeenCalled())

    const expenseBar = await screen.findByRole('button', { name: /Expenses · 2026-07/ })
    await user.click(expenseBar)

    await waitFor(() => expect(api.transactions.list).toHaveBeenCalledWith(expect.objectContaining({
      type: 'debit', from: '2026-07-01', to: '2026-07-31', user_pnl_only: true,
    })))
  })

  it('a donut slice click opens its category over meta.window_from/to', async () => {
    api.reports.incomeExpenses.mockResolvedValue(incomeExpensesWithBars())
    const { user } = renderWithProviders(<ReportsPage />, { route: '/reports?tab=income_expenses' })
    await waitFor(() => expect(api.reports.incomeExpenses).toHaveBeenCalled())

    const slice = await screen.findByRole('button', { name: /Groceries/ })
    await user.click(slice)

    await waitFor(() => expect(api.transactions.list).toHaveBeenCalledWith(expect.objectContaining({
      category_id: 'groceries', type: 'debit', from: '2026-07-01', to: '2026-07-31', user_pnl_only: true,
    })))
  })

  it('a net worth trend click never opens Transactions — it goes to the Accounts list (a balance, not a flow)', async () => {
    const { user } = renderWithProviders(<ReportsPage />, { route: '/reports?tab=net_worth' })
    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalled())

    await user.click(await screen.findByRole('button', { name: t('reports.viewAccountsAria') }))

    expect(navigateMock).toHaveBeenCalledWith('/accounts')
    expect(api.transactions.list).not.toHaveBeenCalled()
  })
})

describe('Reports page — compare with previous period (plan S6)', () => {
  it('fetches the previous window with exact start_date/end_date and labels it', async () => {
    const current: ReportResponse = {
      summary: { primary_value: 400, change_amount: 400, change_percent: null, breakdowns: [] },
      trend: [{ date: '2026-07', value: 400, breakdowns: {}, change: null }],
      meta: { type: 'income_expenses', series_keys: [], currency: 'USD', interval: 'monthly', window_from: '2026-07-01', window_to: '2026-07-31' },
      composition: [],
      category_trend: [],
    }
    api.reports.incomeExpenses.mockResolvedValue(current)
    const { user } = renderWithProviders(<ReportsPage />, { route: '/reports?tab=income_expenses' })
    await waitFor(() => expect(api.reports.incomeExpenses).toHaveBeenCalledTimes(1))

    const expectedPrevious = previousWindow('2026-07-01', '2026-07-31')
    const previous: ReportResponse = {
      ...current,
      meta: { ...current.meta, window_from: expectedPrevious.from, window_to: expectedPrevious.to },
      trend: [{ date: '2026-06', value: 200, breakdowns: {}, change: null }],
    }
    api.reports.incomeExpenses.mockResolvedValueOnce(previous)

    await user.click(await screen.findByRole('button', { name: t('reports.comparePreviousPeriod') }))

    await waitFor(() => {
      const call = api.reports.incomeExpenses.mock.calls.at(-1)!
      expect(call.slice(-2)).toEqual([expectedPrevious.from, expectedPrevious.to])
    })

    const fmt = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
    expect(await screen.findByText(
      t('reports.previousPeriodLabel', { from: fmt(expectedPrevious.from), to: fmt(expectedPrevious.to) }),
    )).toBeInTheDocument()
  })
})

describe('Reports page — categories creeping up (plan R2/S6)', () => {
  function incomeExpensesWithCreepingCategory(): ReportResponse {
    const baselineMonths = ['2026-02', '2026-03', '2026-04', '2026-05', '2026-06', '2026-07']
    const series = [
      ...baselineMonths.map((date) => ({ date, value: 400, breakdowns: {}, change: null })),
      { date: '2026-08', value: 604, breakdowns: {}, change: null },
    ]
    return {
      summary: { primary_value: 400, change_amount: 400, change_percent: null, breakdowns: [] },
      trend: [{ date: '2026-08', value: 400, breakdowns: {}, change: null }],
      meta: { type: 'income_expenses', series_keys: [], currency: 'USD', interval: 'monthly', window_from: '2026-02-01', window_to: '2026-08-31' },
      composition: [],
      category_trend: [
        { key: 'travel', label: 'Travel', color: '#F43F5E', total: 604, group: 'expenses', series },
      ],
    }
  }

  it('shows a creeping-up category and drills into its exact month on click', async () => {
    api.reports.incomeExpenses.mockResolvedValue(incomeExpensesWithCreepingCategory())
    const { user } = renderWithProviders(<ReportsPage />, { route: '/reports?tab=income_expenses' })
    await waitFor(() => expect(api.reports.incomeExpenses).toHaveBeenCalled())

    expect(await screen.findByText(t('reports.creepingUp'))).toBeInTheDocument()
    const row = screen.getByRole('button', { name: /Travel.*over usual/ })
    await user.click(row)

    await waitFor(() => expect(api.transactions.list).toHaveBeenCalledWith(expect.objectContaining({
      category_id: 'travel', type: 'debit', from: '2026-08-01', to: '2026-08-31', user_pnl_only: true,
    })))
  })
})
