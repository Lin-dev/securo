import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'

import { CashTab } from '@/components/wealth/cash/cash-tab'
import { formatMoney } from '@/lib/chart-format'
import {
  wealthCashForecastFixture,
  wealthSubscriptionsFixture,
  wealthUpcomingFixture,
} from '@/test/fixtures/wealth'
import { renderWithProviders, t } from '@/test/utils'
import type { WealthUpcoming } from '@/types/wealth'

const api = vi.hoisted(() => ({
  wealth: {
    cashForecast: vi.fn(),
    upcoming: vi.fn(),
    subscriptions: vi.fn(),
  },
  recurring: { create: vi.fn() },
}))
vi.mock('@/lib/api', () => api)

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
import { toast } from 'sonner'

const privacy = vi.hoisted(() => ({ privacyMode: false }))
const MASK = '•••••'
vi.mock('@/hooks/use-privacy-mode', () => ({
  usePrivacyMode: () => ({
    mask: (value: string) => (privacy.privacyMode ? MASK : value),
    privacyMode: privacy.privacyMode,
    MASK,
  }),
}))

function money(value: number, options: { compact?: boolean } = {}) {
  return formatMoney(value, 'USD', { locale: 'en-US', ...options })
}

beforeEach(() => {
  vi.clearAllMocks()
  privacy.privacyMode = false
  api.wealth.cashForecast.mockResolvedValue(wealthCashForecastFixture)
  api.wealth.upcoming.mockResolvedValue(wealthUpcomingFixture)
  api.wealth.subscriptions.mockResolvedValue(wealthSubscriptionsFixture)
})

describe('CashTab forecast', () => {
  it('states the takeaway title, draws the buffer line, and marks the low point', async () => {
    const { container } = renderWithProviders(<CashTab />)

    expect(await screen.findByText(t('wealthCash.forecast.title.ok', { days: 90 }))).toBeInTheDocument()
    expect(screen.getByText(t('wealthCash.forecast.bufferLabel'))).toBeInTheDocument()
    expect(container.querySelector('.recharts-reference-dot')).not.toBeNull()
    expect(
      screen.getByText(t('wealthCash.forecast.lowPointLabel', { amount: money(18800, { compact: true }) })),
    ).toBeInTheDocument()
  })

  it('says cash dips below the buffer when first_below_buffer_date is set', async () => {
    api.wealth.cashForecast.mockResolvedValue({
      ...wealthCashForecastFixture,
      first_below_buffer_date: '2026-11-12',
    })

    renderWithProviders(<CashTab />)

    expect(
      await screen.findByText(t('wealthCash.forecast.title.belowBuffer', { date: 'Nov 12' })),
    ).toBeInTheDocument()
  })

  it('has a table fallback with every point', async () => {
    const { user } = renderWithProviders(<CashTab />)
    const title = await screen.findByText(t('wealthCash.forecast.title.ok', { days: 90 }))
    const card = title.closest('[data-slot="card"]') as HTMLElement

    await user.click(within(card).getByRole('button', { name: t('charts.showTable') }))
    const rows = within(within(card).getByRole('table')).getAllByRole('row')
    // Header row + 3 forecast points.
    expect(rows).toHaveLength(4)
  })
})

describe('CashTab timeline', () => {
  it('groups items by date and shows the cash total', async () => {
    renderWithProviders(<CashTab />)

    expect(await screen.findByText('Streaming service')).toBeInTheDocument()
    expect(screen.getAllByText('Card bill').length).toBeGreaterThan(0)
    expect(
      screen.getByText(t('wealthCash.timeline.total', { amount: money(1215.49) })),
    ).toBeInTheDocument()
  })

  it('marks a recurring charge on a card account as "on card"', async () => {
    renderWithProviders(<CashTab />)

    const streaming = await screen.findByText('Streaming service')
    const row = streaming.closest('li')
    expect(row).not.toBeNull()
    expect(within(row as HTMLElement).getByText(t('wealthCash.timeline.onCard'))).toBeInTheDocument()

    const cardBill = screen.getAllByText('Card bill')[0].closest('li')
    expect(cardBill).not.toBeNull()
    expect(within(cardBill as HTMLElement).queryByText(t('wealthCash.timeline.onCard'))).not.toBeInTheDocument()
  })

  it('shows the displayed total as given, excluding a card purchase folded into the card bill', async () => {
    const upcoming: WealthUpcoming = {
      currency: 'USD',
      days: 30,
      items: [
        { date: '2026-10-20', description: 'Streaming service', amount: 15.49, kind: 'recurring', account: 'Credit card', category: 'Subscriptions', recurring_id: 'rec-1', hits_cash: false, estimated: false },
        { date: '2026-10-25', description: 'Card bill', amount: 1200, kind: 'card_bill', account: 'Checking', category: null, recurring_id: null, hits_cash: true, estimated: false },
      ],
      // Only the card bill hits cash -- the streaming charge is folded into it.
      total: 1200,
    }
    api.wealth.upcoming.mockResolvedValue(upcoming)

    renderWithProviders(<CashTab />)

    await screen.findByText('Streaming service')
    expect(screen.getByText(t('wealthCash.timeline.total', { amount: money(1200) }))).toBeInTheDocument()
    expect(screen.queryByText(t('wealthCash.timeline.total', { amount: money(1215.49) }))).not.toBeInTheDocument()
  })

  it('links a recurring_id item to its rule, and not an item without one', async () => {
    renderWithProviders(<CashTab />)

    const streaming = await screen.findByText('Streaming service')
    const streamingRow = streaming.closest('li') as HTMLElement
    const link = within(streamingRow).getByRole('link', { name: t('wealthCash.timeline.viewRule') })
    expect(link).toHaveAttribute('href', '/recurring')

    const cardBillRow = screen.getAllByText('Card bill')[0].closest('li') as HTMLElement
    expect(within(cardBillRow).queryByRole('link')).not.toBeInTheDocument()
  })
})

describe('CashTab subscriptions', () => {
  it('renders the tracked subscriptions and their totals', async () => {
    renderWithProviders(<CashTab />)

    expect(await screen.findByText('STREAMING CO')).toBeInTheDocument()
    expect(
      screen.getByText(t('wealthCash.subscriptions.monthlyTotal', { amount: money(15.49) })),
    ).toBeInTheDocument()
    expect(
      screen.getByText(t('wealthCash.subscriptions.annualTotal', { amount: money(185.88) })),
    ).toBeInTheDocument()
  })

  it('"Add as recurring" posts the exact create_payload and invalidates recurring and wealth queries', async () => {
    api.recurring.create.mockResolvedValue({ id: 'new-rec' })
    const { user, queryClient } = renderWithProviders(<CashTab />)
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
    api.recurring.create.mockRejectedValue(new Error('boom'))
    const { user } = renderWithProviders(<CashTab />)

    await screen.findByText('GYM CLUB')
    await user.click(screen.getByRole('button', { name: t('wealthCash.suggestions.addAction') }))

    await waitFor(() => expect(toast.error).toHaveBeenCalled())
  })
})

describe('CashTab privacy mode', () => {
  it('masks amounts across the forecast, timeline and subscriptions', async () => {
    privacy.privacyMode = true
    renderWithProviders(<CashTab />)

    await screen.findByText('Streaming service')
    expect(screen.getAllByText(MASK).length).toBeGreaterThan(0)
    expect(screen.queryByText(money(1215.49))).not.toBeInTheDocument()
    expect(screen.queryByText(money(15.49))).not.toBeInTheDocument()
  })
})
