import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'

import DashboardPage from '@/pages/dashboard'
import { TooltipProvider } from '@/components/ui/tooltip'
import { formatCurrency } from '@/lib/format'
import { renderWithProviders, t } from '@/test/utils'
import { wealthSavingsRateFixture, wealthSummaryFixture } from '@/test/fixtures/wealth'
import type { Account, DashboardSummary, GoalSummary } from '@/types'

const api = vi.hoisted(() => ({
  dashboard: {
    summary: vi.fn(),
    spendingByCategory: vi.fn(),
    balanceHistory: vi.fn(),
    projectedTransactions: vi.fn(),
  },
  transactions: { list: vi.fn(), calendar: vi.fn() },
  budgets: { comparison: vi.fn() },
  categories: { list: vi.fn() },
  categoryGroups: { list: vi.fn() },
  accounts: { list: vi.fn() },
  goals: { summary: vi.fn() },
  groups: { list: vi.fn() },
  payees: { list: vi.fn() },
  rules: { create: vi.fn() },
  wealth: { summary: vi.fn(), savingsRate: vi.fn() },
}))

vi.mock('@/lib/api', () => api)

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

const privacy = vi.hoisted(() => ({ privacyMode: false }))
const MASK = '••••'
vi.mock('@/hooks/use-privacy-mode', () => ({
  usePrivacyMode: () => ({
    mask: (value: string) => (privacy.privacyMode ? MASK : value),
    privacyMode: privacy.privacyMode,
    MASK,
  }),
}))

vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }),
}))

vi.mock('@/contexts/collection-filter-context', () => ({
  useCollectionFilter: () => ({ activeAccountIds: null, activeWalletIds: null }),
}))

// Default off, same as a workspace that never got the `wealth` module. Tests
// that need it on call `workspace.hasModule.mockImplementation(...)`.
const workspace = vi.hoisted(() => ({ hasModule: vi.fn<(id: string) => boolean>(() => false) }))
vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: () => workspace }))

const timezone = vi.hoisted(() => ({ value: undefined as string | undefined }))
vi.mock('@/hooks/use-timezone', () => ({ useEffectiveTimezone: () => timezone.value }))

function account(overrides: Partial<Account>): Account {
  return {
    id: 'acc',
    user_id: 'user-1',
    connection_id: null,
    external_id: null,
    name: 'Account',
    display_name: null,
    masked_number: null,
    institution_name: null,
    institution_logo_url: null,
    type: 'checking',
    balance: 0,
    current_balance: 0,
    previous_balance: null,
    balance_primary: null,
    currency: 'USD',
    credit_limit: null,
    available_credit: null,
    statement_close_day: null,
    payment_due_day: null,
    next_close_date: null,
    next_due_date: null,
    minimum_payment: null,
    card_brand: null,
    card_level: null,
    shared_balance_group: null,
    is_closed: false,
    closed_at: null,
    ...overrides,
  }
}

function goalSummary(overrides: Partial<GoalSummary>): GoalSummary {
  return {
    id: 'goal-1',
    name: 'Millionaire',
    target_amount: 1000000,
    current_amount: 500000,
    currency: 'USD',
    target_date: '2032-01-01',
    status: 'active',
    icon: null,
    color: null,
    percentage: 50,
    monthly_contribution: null,
    on_track: null,
    ...overrides,
  }
}

// Two cards on one shared credit line both report the line's full -300.
const accounts = [
  account({ id: 'checking', name: 'Checking', current_balance: 1000 }),
  account({ id: 'card-a', name: 'Card A', type: 'credit_card', current_balance: -300, shared_balance_group: 'line-1' }),
  account({ id: 'card-b', name: 'Card B', type: 'credit_card', current_balance: -300, shared_balance_group: 'line-1' }),
  account({ id: 'card-c', name: 'Card C', type: 'credit_card', current_balance: -200 }),
]

const summary: DashboardSummary = {
  total_balance: { USD: 500 },
  total_balance_primary: 500,
  projected_balance: { USD: 500 },
  projected_balance_primary: 500,
  balance_date: '2026-09-12',
  monthly_income: 0,
  monthly_expenses: 0,
  monthly_income_primary: 0,
  monthly_expenses_primary: 0,
  accounts_count: accounts.length,
  pending_categorization: 0,
  pending_categorization_amount: 0,
  assets_value: {},
  assets_value_primary: 0,
  primary_currency: 'USD',
  pending_shares_net: 0,
}

beforeEach(() => {
  vi.clearAllMocks()
  privacy.privacyMode = false
  timezone.value = undefined
  workspace.hasModule.mockReturnValue(false)
  api.dashboard.summary.mockResolvedValue(summary)
  api.dashboard.spendingByCategory.mockResolvedValue([])
  api.dashboard.balanceHistory.mockResolvedValue({ current: [], previous: [] })
  api.dashboard.projectedTransactions.mockResolvedValue([])
  api.transactions.list.mockResolvedValue({ items: [], total: 0 })
  api.budgets.comparison.mockResolvedValue([])
  api.categories.list.mockResolvedValue([])
  api.categoryGroups.list.mockResolvedValue([])
  api.accounts.list.mockResolvedValue(accounts)
  api.goals.summary.mockResolvedValue([])
  api.groups.list.mockResolvedValue([])
  api.payees.list.mockResolvedValue([])
  api.wealth.summary.mockResolvedValue(wealthSummaryFixture)
  api.wealth.savingsRate.mockResolvedValue(wealthSavingsRateFixture)
})

describe('Dashboard net worth breakdown', () => {
  it('counts a shared credit line once in the credit card total', async () => {
    const { user } = renderWithProviders(
      <TooltipProvider delayDuration={0}>
        <DashboardPage />
      </TooltipProvider>,
    )

    await screen.findByText(formatCurrency(500, 'USD', 'en-US'))
    await user.hover(screen.getByRole('button', { name: 'i' }))

    const tooltip = await screen.findByRole('tooltip')
    const cardRow = within(tooltip).getByText(t('dashboard.creditCardBalance')).parentElement!
    expect(cardRow).toHaveTextContent(formatCurrency(-500, 'USD', 'en-US'))
  })
})

describe('Dashboard wealth module gating', () => {
  it('renders exactly as before and never calls the wealth endpoints when the module is off', async () => {
    renderWithProviders(
      <TooltipProvider delayDuration={0}>
        <DashboardPage />
      </TooltipProvider>,
    )

    await screen.findByText(formatCurrency(500, 'USD', 'en-US'))

    expect(api.wealth.summary).not.toHaveBeenCalled()
    expect(api.wealth.savingsRate).not.toHaveBeenCalled()
    expect(screen.queryByText(t('dashboard.wealthCashCushionTitle'))).not.toBeInTheDocument()
  })
})

describe('Dashboard savings-rate tile', () => {
  it('shows the P1 rate for the selected month when the module is on and that month is in the series', async () => {
    workspace.hasModule.mockImplementation((id: string) => id === 'wealth')

    renderWithProviders(
      <TooltipProvider delayDuration={0}>
        <DashboardPage />
      </TooltipProvider>,
      { route: '/?month=2026-09' },
    )

    // wealthSavingsRateFixture's 2026-09-01 row has rate: 0.294 -> 29%.
    await screen.findByText('29%')
    expect(screen.getByText(t('dashboard.wealthSavingsRateCaption'))).toBeInTheDocument()
    expect(screen.queryByText(t('dashboard.savingsRateCaption'))).not.toBeInTheDocument()
  })

  it('falls back to the existing (income - expenses) / income figure when the month is missing from the series', async () => {
    workspace.hasModule.mockImplementation((id: string) => id === 'wealth')
    api.dashboard.summary.mockResolvedValue({
      ...summary,
      monthly_income: 1000,
      monthly_expenses: 800,
      monthly_income_primary: 1000,
      monthly_expenses_primary: 800,
    })

    renderWithProviders(
      <TooltipProvider delayDuration={0}>
        <DashboardPage />
      </TooltipProvider>,
      // Not one of wealthSavingsRateFixture's months (2026-08/09/10).
      { route: '/?month=2026-01' },
    )

    await screen.findByText('20%')
    expect(screen.getByText(t('dashboard.savingsRateCaption'))).toBeInTheDocument()
    expect(screen.queryByText(t('dashboard.wealthSavingsRateCaption'))).not.toBeInTheDocument()
  })

  it('falls back to the existing figure when the module is off, even for a month that is in the fixture series', async () => {
    api.dashboard.summary.mockResolvedValue({
      ...summary,
      monthly_income: 1000,
      monthly_expenses: 800,
      monthly_income_primary: 1000,
      monthly_expenses_primary: 800,
    })

    renderWithProviders(
      <TooltipProvider delayDuration={0}>
        <DashboardPage />
      </TooltipProvider>,
      { route: '/?month=2026-09' },
    )

    await screen.findByText('20%')
    expect(screen.getByText(t('dashboard.savingsRateCaption'))).toBeInTheDocument()
  })
})

describe('Dashboard cash-cushion card', () => {
  it('shows the cash-cushion months and idle cash above the buffer', async () => {
    workspace.hasModule.mockImplementation((id: string) => id === 'wealth')

    renderWithProviders(
      <TooltipProvider delayDuration={0}>
        <DashboardPage />
      </TooltipProvider>,
    )

    // wealthSummaryFixture: cash_cushion_months: 6, idle_cash: 20000, USD.
    await screen.findByText(t('dashboard.wealthCashCushionMonths', { months: '6.0' }))
    expect(
      screen.getByText(t('dashboard.wealthIdleCashAboveBuffer', { amount: formatCurrency(20000, 'USD', 'en-US') })),
    ).toBeInTheDocument()
  })

  it('masks both the cash-cushion months and the idle cash in privacy mode', async () => {
    workspace.hasModule.mockImplementation((id: string) => id === 'wealth')
    privacy.privacyMode = true

    renderWithProviders(
      <TooltipProvider delayDuration={0}>
        <DashboardPage />
      </TooltipProvider>,
    )

    await screen.findByText(t('dashboard.wealthCashCushionMonths', { months: MASK }))
    expect(
      screen.getByText(t('dashboard.wealthIdleCashAboveBuffer', { amount: MASK })),
    ).toBeInTheDocument()
    expect(screen.queryByText(t('dashboard.wealthCashCushionMonths', { months: '6.0' }))).not.toBeInTheDocument()
    expect(
      screen.queryByText(t('dashboard.wealthIdleCashAboveBuffer', { amount: formatCurrency(20000, 'USD', 'en-US') })),
    ).not.toBeInTheDocument()
  })
})

describe('Dashboard goals card projected date', () => {
  it('shows the projected quarter for a net-worth goal when wealth.summary().precision is "quarter"', async () => {
    workspace.hasModule.mockImplementation((id: string) => id === 'wealth')
    api.goals.summary.mockResolvedValue([
      goalSummary({ projected_date: '2030-03-01' }),
    ])

    renderWithProviders(
      <TooltipProvider delayDuration={0}>
        <DashboardPage />
      </TooltipProvider>,
    )

    await screen.findByText(t('dashboard.wealthGoalProjected', { label: 'Q1 2030' }))
  })

  it('omits the projected line for a goal with no projected_date', async () => {
    workspace.hasModule.mockImplementation((id: string) => id === 'wealth')
    api.goals.summary.mockResolvedValue([goalSummary({ projected_date: null })])

    renderWithProviders(
      <TooltipProvider delayDuration={0}>
        <DashboardPage />
      </TooltipProvider>,
    )

    await screen.findByText('Millionaire')
    expect(screen.queryByText(/Projected:/)).not.toBeInTheDocument()
  })

  it('omits the projected line when the module is off, even if the goal carries a projected_date', async () => {
    api.goals.summary.mockResolvedValue([goalSummary({ projected_date: '2030-03-01' })])

    renderWithProviders(
      <TooltipProvider delayDuration={0}>
        <DashboardPage />
      </TooltipProvider>,
    )

    await screen.findByText('Millionaire')
    expect(screen.queryByText(/Projected:/)).not.toBeInTheDocument()
  })
})
