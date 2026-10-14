import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'

import TransactionsPage from '@/pages/transactions'
import { renderWithProviders } from '@/test/utils'
import type { Category, CategoryGroup, PaginatedTransactions } from '@/types'

const api = vi.hoisted(() => ({
  transactions: {
    list: vi.fn(),
    calendar: vi.fn(),
    export: vi.fn(),
  },
  categories: {
    list: vi.fn(),
    listIncludingHidden: vi.fn(),
  },
  categoryGroups: { list: vi.fn() },
  accounts: { list: vi.fn() },
  recurring: { list: vi.fn() },
  payees: { list: vi.fn() },
  admin: { accountingMode: vi.fn() },
  groups: { list: vi.fn(), get: vi.fn() },
  rules: { create: vi.fn() },
  reconciliation: { suggestions: vi.fn() },
}))
vi.mock('@/lib/api', () => api)

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }),
}))

const workspace = vi.hoisted(() => ({
  hasModule: vi.fn<(id: string) => boolean>(() => false),
  canWrite: true,
}))
vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: () => workspace }))

vi.mock('@/contexts/collection-filter-context', () => ({
  useCollectionFilter: () => ({
    collections: [],
    activeCollectionId: null,
    activeCollection: null,
    setActiveCollectionId: vi.fn(),
    activeAccountIds: null,
    activeWalletIds: null,
  }),
}))

const EMPTY_PAGE: PaginatedTransactions = { items: [], total: 0, page: 1, limit: 20 }

const CATEGORY_RENT: Category = {
  id: 'cat-rent',
  user_id: 'u1',
  group_id: 'grp-housing',
  name: 'Rent',
  icon: 'home',
  color: '#F43F5E',
  is_system: false,
  is_hidden: false,
  treat_as_transfer: false,
  is_ignored: false,
}

const GROUP_HOUSING: CategoryGroup = {
  id: 'grp-housing',
  user_id: 'u1',
  name: 'Housing',
  icon: 'home',
  color: '#F43F5E',
  position: 0,
  is_system: false,
  is_hidden: false,
  categories: [],
}

function renderPage(route: string) {
  return renderWithProviders(<TransactionsPage />, { route })
}

describe('TransactionsPage wealth_pnl_only', () => {
  beforeEach(() => {
    vi.mocked(api.transactions.list).mockResolvedValue(EMPTY_PAGE as never)
    vi.mocked(api.transactions.calendar).mockResolvedValue({ days: [] } as never)
    vi.mocked(api.categories.list).mockResolvedValue([CATEGORY_RENT] as never)
    vi.mocked(api.categories.listIncludingHidden).mockResolvedValue([CATEGORY_RENT] as never)
    vi.mocked(api.categoryGroups.list).mockResolvedValue([GROUP_HOUSING] as never)
    vi.mocked(api.accounts.list).mockResolvedValue([] as never)
    vi.mocked(api.recurring.list).mockResolvedValue([] as never)
    vi.mocked(api.payees.list).mockResolvedValue([] as never)
    vi.mocked(api.admin.accountingMode).mockResolvedValue({ mode: 'cash' } as never)
    vi.mocked(api.groups.list).mockResolvedValue([] as never)
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('sends wealth_pnl_only=true when the URL has it', async () => {
    renderPage('/transactions?wealth_pnl_only=1&from=2026-10-01&to=2026-10-31')

    await waitFor(() => {
      expect(api.transactions.list).toHaveBeenCalledWith(expect.objectContaining({
        wealth_pnl_only: true,
      }))
    })
  })

  it('does not send wealth_pnl_only when the URL omits it', async () => {
    renderPage('/transactions?from=2026-10-01&to=2026-10-31')

    await waitFor(() => {
      expect(api.transactions.list).toHaveBeenCalled()
    })
    for (const call of vi.mocked(api.transactions.list).mock.calls) {
      expect((call[0] as { wealth_pnl_only?: boolean })?.wealth_pnl_only).toBeUndefined()
    }
  })

  it('shows a removable "Wealth P&L only" chip, and clearing it drops the param', async () => {
    renderPage('/transactions?wealth_pnl_only=1&from=2026-10-01&to=2026-10-31')

    const chip = await screen.findByText('Wealth P&L only')
    expect(chip).toBeInTheDocument()

    await waitFor(() => {
      expect(api.transactions.list).toHaveBeenCalledWith(expect.objectContaining({ wealth_pnl_only: true }))
    })
    vi.mocked(api.transactions.list).mockClear()

    const clearButton = screen.getByRole('button', { name: 'Clear P&L-only filter' })
    clearButton.click()

    await waitFor(() => {
      expect(screen.queryByText('Wealth P&L only')).toBeNull()
    })
    await waitFor(() => {
      const lastCall = vi.mocked(api.transactions.list).mock.calls.at(-1)
      expect((lastCall?.[0] as { wealth_pnl_only?: boolean } | undefined)?.wealth_pnl_only).toBeUndefined()
    })
  })

  it('has no chip, and sends no wealth_pnl_only, without the URL param', async () => {
    renderPage('/transactions?from=2026-10-01&to=2026-10-31')

    await waitFor(() => {
      expect(api.transactions.list).toHaveBeenCalled()
    })
    expect(screen.queryByText('Wealth P&L only')).toBeNull()
  })
})
