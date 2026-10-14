import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'

import BudgetsPage from '@/pages/budgets'
import { formatMoney } from '@/lib/chart-format'
import { monthWindow } from '@/lib/chart-period'
import { transactionsUrl } from '@/lib/drilldown'
import { renderWithProviders, t } from '@/test/utils'
import { wealthBudgetPaceFixture, wealthBudgetSuggestionsFixture } from '@/test/fixtures/wealth'
import type { Budget, BudgetVsActual, Category, CategoryGroup } from '@/types'

const api = vi.hoisted(() => ({
  budgets: {
    list: vi.fn(),
    comparison: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
  },
  categories: { list: vi.fn(), listIncludingHidden: vi.fn() },
  categoryGroups: { list: vi.fn() },
  wealth: {
    budgetSuggestions: vi.fn(),
    applyBudgetSuggestions: vi.fn(),
    budgetPace: vi.fn(),
    updateSettings: vi.fn(),
  },
}))
vi.mock('@/lib/api', () => api)

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
import { toast } from 'sonner'

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
}))

const privacy = vi.hoisted(() => ({ privacyMode: false }))
const MASK = '•••••'
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

const workspace = vi.hoisted(() => ({
  hasModule: vi.fn<(id: string) => boolean>(() => false),
  canWrite: true,
}))
vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: () => workspace }))

const navigate = vi.hoisted(() => vi.fn())
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigate,
}))

function money(value: number) {
  return formatMoney(value, 'USD', { locale: 'en-US' })
}

const CATEGORY_DINING: Category = {
  id: 'cat-dining',
  user_id: 'u1',
  group_id: 'grp-food',
  name: 'Dining out',
  icon: 'utensils',
  color: '#ff0000',
  is_system: false,
  is_hidden: false,
  treat_as_transfer: false,
  is_ignored: false,
}

const CATEGORY_GROCERIES: Category = {
  ...CATEGORY_DINING,
  id: 'cat-groceries',
  name: 'Groceries',
}

const GROUP_FOOD: CategoryGroup = {
  id: 'grp-food',
  user_id: 'u1',
  name: 'Food',
  icon: 'utensils',
  color: '#ff0000',
  position: 0,
  is_system: false,
  is_hidden: false,
  categories: [CATEGORY_DINING, CATEGORY_GROCERIES],
}

function budget(overrides: Partial<Budget> = {}): Budget {
  return {
    id: 'b-dining',
    user_id: 'u1',
    category_id: 'cat-dining',
    amount: 600,
    month: '2026-10-01',
    is_recurring: true,
    ...overrides,
  }
}

function comparisonRow(overrides: Partial<BudgetVsActual> = {}): BudgetVsActual {
  return {
    category_id: 'cat-dining',
    category_name: 'Dining out',
    category_icon: 'utensils',
    category_color: '#ff0000',
    group_id: 'grp-food',
    group_name: 'Food',
    budget_amount: 600,
    actual_amount: 300,
    projected_amount: 620,
    prev_month_amount: 580,
    projected_prev_month_amount: 580,
    percentage_used: 50,
    is_recurring: true,
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  privacy.privacyMode = false
  workspace.hasModule.mockReturnValue(false)
  workspace.canWrite = true
  api.categories.list.mockResolvedValue([CATEGORY_DINING, CATEGORY_GROCERIES])
  api.categories.listIncludingHidden.mockResolvedValue([CATEGORY_DINING, CATEGORY_GROCERIES])
  api.categoryGroups.list.mockResolvedValue([GROUP_FOOD])
  api.budgets.list.mockResolvedValue([])
  api.budgets.comparison.mockResolvedValue([])
  api.wealth.budgetSuggestions.mockResolvedValue(wealthBudgetSuggestionsFixture)
  api.wealth.budgetPace.mockResolvedValue(wealthBudgetPaceFixture)
})

describe('BudgetsPage suggestions panel', () => {
  it('renders the fixture and Create sends exactly the checked ids and shows the result toast', async () => {
    workspace.hasModule.mockImplementation((id: string) => id === 'wealth')
    api.wealth.applyBudgetSuggestions.mockResolvedValue({ created: 1, skipped: 1 })

    const { user } = renderWithProviders(<BudgetsPage />)

    expect(await screen.findByText(t('budgetsPace.suggestions.title', { count: 2 }))).toBeInTheDocument()
    expect(screen.getByText('Dining out')).toBeInTheDocument()
    expect(screen.getByText('Groceries')).toBeInTheDocument()
    // The groceries suggestion already has a budget, so it starts unchecked
    // and is labelled with the existing amount.
    expect(
      screen.getByText(t('budgetsPace.suggestions.existingBudget', { amount: money(400) })),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: t('budgetsPace.suggestions.createButton', { count: 1 }) }))

    await waitFor(() => {
      expect(api.wealth.applyBudgetSuggestions).toHaveBeenCalledWith({ category_ids: ['cat-dining'] })
    })
    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith(
        t('budgetsPace.suggestions.resultToast', { created: 1, skipped: 1 }),
      )
    })
  })
})

describe('BudgetsPage wealth module gating', () => {
  it('hides the suggestions panel and the cap, and calls neither endpoint, when the module is off', async () => {
    api.budgets.list.mockResolvedValue([budget()])
    api.budgets.comparison.mockResolvedValue([comparisonRow()])

    renderWithProviders(<BudgetsPage />)

    await screen.findByText('Dining out')

    expect(screen.queryByText(t('budgetsPace.cap.title'))).not.toBeInTheDocument()
    expect(api.wealth.budgetSuggestions).not.toHaveBeenCalled()
    expect(api.wealth.budgetPace).not.toHaveBeenCalled()
  })
})

describe('BudgetsPage bullet bars', () => {
  it("shows the pace marker and badge for the current month's budget row", async () => {
    api.budgets.list.mockResolvedValue([budget()])
    api.budgets.comparison.mockResolvedValue([
      comparisonRow({
        expected_to_date: 280,
        projected_month_end: 600,
        pace_status: 'over',
        left_to_spend: 300,
        left_per_day: 20,
        days_elapsed: 14,
        days_in_month: 31,
      }),
    ])

    renderWithProviders(<BudgetsPage />)

    await screen.findByText('Dining out')
    expect(screen.getByText(t('budgetsPace.status.over'))).toBeInTheDocument()
    expect(screen.getByText(new RegExp(`Expected by now: ${escapeRegex(money(280))}`))).toBeInTheDocument()
    expect(
      screen.getByText(t('budgetsPace.leftToSpend', { amount: money(300) })),
    ).toBeInTheDocument()
    expect(
      screen.getByText(t('budgetsPace.leftPerDay', { amount: money(20) })),
    ).toBeInTheDocument()
  })

  it('shows no marker or badge for a past month with no pace fields', async () => {
    api.budgets.list.mockResolvedValue([budget()])
    api.budgets.comparison.mockResolvedValue([comparisonRow()])

    renderWithProviders(<BudgetsPage />)

    await screen.findByText('Dining out')
    expect(screen.queryByText(/Expected by now/)).not.toBeInTheDocument()
    expect(screen.queryByText(t('budgetsPace.status.over'))).not.toBeInTheDocument()
    expect(screen.queryByText(t('budgetsPace.status.under'))).not.toBeInTheDocument()
    // The pre-existing projected/previous-month figures still show.
    expect(screen.getByText(t('budgetsPace.projected', { amount: money(620) }))).toBeInTheDocument()
    expect(screen.getByText(t('budgetsPace.lastMonth', { amount: money(580) }))).toBeInTheDocument()
  })
})

describe('BudgetsPage spending cap', () => {
  it('setting the cap calls updateSettings({ spending_cap })', async () => {
    workspace.hasModule.mockImplementation((id: string) => id === 'wealth')
    api.wealth.budgetPace.mockResolvedValue({ ...wealthBudgetPaceFixture, cap: null })
    api.wealth.updateSettings.mockResolvedValue({})

    const { user } = renderWithProviders(<BudgetsPage />)

    await user.click(await screen.findByRole('button', { name: t('budgetsPace.cap.setAction') }))
    const input = screen.getByLabelText(t('budgetsPace.cap.inputLabel'))
    await user.clear(input)
    await user.type(input, '500')
    await user.click(screen.getByRole('button', { name: t('common.save') }))

    await waitFor(() => {
      expect(api.wealth.updateSettings).toHaveBeenCalledWith({ spending_cap: 500 })
    })
  })

  it('clearing the cap calls updateSettings({ clear_spending_cap: true })', async () => {
    workspace.hasModule.mockImplementation((id: string) => id === 'wealth')
    api.wealth.updateSettings.mockResolvedValue({})

    const { user } = renderWithProviders(<BudgetsPage />)

    await user.click(await screen.findByRole('button', { name: t('budgetsPace.cap.clear') }))

    await waitFor(() => {
      expect(api.wealth.updateSettings).toHaveBeenCalledWith({ clear_spending_cap: true })
    })
  })
})

describe('BudgetsPage drill-down', () => {
  it('clicking a budget row navigates to the right Transactions URL', async () => {
    api.budgets.list.mockResolvedValue([budget()])
    api.budgets.comparison.mockResolvedValue([comparisonRow()])

    const { user } = renderWithProviders(<BudgetsPage />)

    const row = await screen.findByRole('button', {
      name: t('budgetsPace.row.viewTransactions', { category: 'Dining out' }),
    })
    await user.click(row)

    // The page's own selected month drives the window -- not the budget
    // row's `month` field -- so compute it the same way the page does.
    const now = new Date()
    const monthParam = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-01`
    expect(navigate).toHaveBeenCalledWith(
      transactionsUrl({ ...monthWindow(monthParam), categoryId: 'cat-dining' }),
    )
  })
})

describe('BudgetsPage privacy mode', () => {
  it('masks amounts', async () => {
    privacy.privacyMode = true
    api.budgets.list.mockResolvedValue([budget()])
    api.budgets.comparison.mockResolvedValue([comparisonRow()])

    renderWithProviders(<BudgetsPage />)

    await screen.findByText('Dining out')
    expect(screen.queryByText(money(300))).not.toBeInTheDocument()
    expect(screen.queryByText(money(620))).not.toBeInTheDocument()
    expect(screen.getAllByText(MASK).length).toBeGreaterThan(0)
  })
})

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}
