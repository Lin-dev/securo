import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'

import RecurringPage from '@/pages/recurring'
import { formatMoney } from '@/lib/chart-format'
import { wealthSubscriptionsFixture } from '@/test/fixtures/wealth'
import { renderWithProviders, t } from '@/test/utils'

const api = vi.hoisted(() => ({
  categories: { list: vi.fn(), listIncludingHidden: vi.fn() },
  categoryGroups: { list: vi.fn() },
  recurring: {
    list: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
    generate: vi.fn(),
  },
  accounts: { list: vi.fn() },
  currencies: { list: vi.fn() },
  wealth: { subscriptions: vi.fn() },
}))
vi.mock('@/lib/api', () => api)

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
import { toast } from 'sonner'

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

function money(value: number) {
  return formatMoney(value, 'USD', { locale: 'en-US' })
}

beforeEach(() => {
  vi.clearAllMocks()
  workspace.hasModule.mockReturnValue(false)
  workspace.canWrite = true
  api.recurring.list.mockResolvedValue([])
  api.categories.list.mockResolvedValue([])
  api.categoryGroups.list.mockResolvedValue([])
  api.accounts.list.mockResolvedValue([])
  api.wealth.subscriptions.mockResolvedValue(wealthSubscriptionsFixture)
})

describe('RecurringPage wealth module gating', () => {
  it('shows the "Not tracked yet" suggestions card when the module is on', async () => {
    workspace.hasModule.mockImplementation((id: string) => id === 'wealth')

    renderWithProviders(<RecurringPage />)

    expect(await screen.findByText('GYM CLUB')).toBeInTheDocument()
    expect(screen.getByText(t('wealthCash.suggestions.title'))).toBeInTheDocument()
    expect(
      screen.getByText(t('wealthCash.suggestions.detail', { amount: money(40), count: 3 })),
    ).toBeInTheDocument()
  })

  it('hides the card and never calls the endpoint when the module is off', async () => {
    renderWithProviders(<RecurringPage />)

    await screen.findByText(t('recurring.empty'))
    expect(screen.queryByText(t('wealthCash.suggestions.title'))).not.toBeInTheDocument()
    expect(api.wealth.subscriptions).not.toHaveBeenCalled()
  })
})

describe('RecurringPage "Add as recurring"', () => {
  it('posts the exact create_payload and invalidates recurring and wealth queries', async () => {
    workspace.hasModule.mockImplementation((id: string) => id === 'wealth')
    api.recurring.create.mockResolvedValue({ id: 'new-rec' })

    const { user, queryClient } = renderWithProviders(<RecurringPage />)
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

    await screen.findByText('GYM CLUB')
    await user.click(screen.getByRole('button', { name: t('wealthCash.suggestions.addAction') }))

    await waitFor(() => {
      expect(api.recurring.create).toHaveBeenCalledWith({
        description: 'GYM CLUB',
        amount: 40,
        type: 'debit',
        frequency: 'monthly',
      })
    })
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith(t('recurring.created')))

    const invalidatedKeys = invalidateSpy.mock.calls.map((call) => (call[0] as { queryKey: unknown[] })?.queryKey?.[0])
    expect(invalidatedKeys).toContain('recurring')
    expect(invalidatedKeys).toContain('wealth')
  })

  it('shows an error toast when creating the suggestion fails', async () => {
    workspace.hasModule.mockImplementation((id: string) => id === 'wealth')
    api.recurring.create.mockRejectedValue(new Error('boom'))

    const { user } = renderWithProviders(<RecurringPage />)

    await screen.findByText('GYM CLUB')
    await user.click(screen.getByRole('button', { name: t('wealthCash.suggestions.addAction') }))

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(t('common.error')))
  })
})
