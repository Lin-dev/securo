import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'

import { RoadTab } from '@/components/wealth/road/road-tab'
import { getPageChatContext } from '@/lib/page-chat-context'
import { formatMoney } from '@/lib/chart-format'
import {
  wealthLeversFixture,
  wealthNetWorthHistoryFixture,
  wealthProjectionFixture,
  wealthSummaryFixture,
} from '@/test/fixtures/wealth'
import { renderWithProviders, t } from '@/test/utils'
import type { WealthLevers, WealthSummary } from '@/types/wealth'

const api = vi.hoisted(() => ({
  wealth: {
    summary: vi.fn(),
    projection: vi.fn(),
    levers: vi.fn(),
    netWorthHistory: vi.fn(),
    updateSettings: vi.fn(),
  },
  goals: { create: vi.fn() },
}))

vi.mock('@/lib/api', () => api)
vi.mock('@/hooks/use-display-locale', () => ({ useDisplayLocale: () => 'en-US' }))

const PRIVACY_MASK = '•••••'

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function money(value: number) {
  return formatMoney(value, 'USD', { locale: 'en-US' })
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  api.wealth.summary.mockResolvedValue(clone(wealthSummaryFixture))
  api.wealth.projection.mockResolvedValue(clone(wealthProjectionFixture))
  api.wealth.levers.mockResolvedValue(clone(wealthLeversFixture))
  api.wealth.netWorthHistory.mockResolvedValue(clone(wealthNetWorthHistoryFixture))
})

describe('RoadTab hero', () => {
  it("renders the fixture's projected label and the days-of-data note", async () => {
    renderWithProviders(<RoadTab />)

    expect(await screen.findByText(t('wealthRoad.hero.projected', { label: 'Q1 2030' }))).toBeInTheDocument()
    expect(screen.getByText(t('wealthRoad.hero.coverage', { days: 94 }))).toBeInTheDocument()
  })

  it('offers to create the $1M goal when there is none, then links it to wealth settings', async () => {
    const noGoal: WealthSummary = { ...clone(wealthSummaryFixture), goal: null }
    api.wealth.summary.mockResolvedValue(noGoal)
    api.goals.create.mockResolvedValue({ id: 'goal-new', name: 'Millionaire' })
    api.wealth.updateSettings.mockResolvedValue({})

    const { user } = renderWithProviders(<RoadTab />)

    const button = await screen.findByRole('button', { name: t('wealthRoad.hero.createGoal') })
    await user.click(button)

    await waitFor(() =>
      expect(api.goals.create).toHaveBeenCalledWith({
        name: t('wealthRoad.hero.createGoalName'),
        target_amount: 1_000_000,
        currency: 'USD',
        tracking_type: 'net_worth',
      }),
    )
    await waitFor(() => expect(api.wealth.updateSettings).toHaveBeenCalledWith({ goal_id: 'goal-new' }))
  })
})

describe('RoadTab levers', () => {
  it('sorts by most months saved first and shows the idle-cash amount', async () => {
    const { container } = renderWithProviders(<RoadTab />)

    const idleCashText = t('wealthRoad.levers.idleCash', { amount: money(26000) })
    const plusText = t('wealthRoad.levers.plusMonthly', { amount: money(500) })
    const returnText = t('wealthRoad.levers.returnLower')

    expect(await screen.findByText(idleCashText)).toBeInTheDocument()
    expect(screen.getByText(plusText)).toBeInTheDocument()
    expect(screen.getByText(returnText)).toBeInTheDocument()

    const text = container.textContent ?? ''
    expect(text.indexOf(idleCashText)).toBeLessThan(text.indexOf(plusText))
    expect(text.indexOf(plusText)).toBeLessThan(text.indexOf(returnText))
  })

  it('formats the date-shift line for a lever', async () => {
    renderWithProviders(<RoadTab />)

    expect(await screen.findByText(t('wealthRoad.levers.shift', { months: '−4', label: 'Q2 2033' }))).toBeInTheDocument()
  })
})

describe('RoadTab privacy mode', () => {
  it('masks the hero amount and the this-month move text', async () => {
    localStorage.setItem('privacyMode', 'true')

    renderWithProviders(<RoadTab />)

    expect(await screen.findAllByText(PRIVACY_MASK)).not.toHaveLength(0)
    expect(screen.queryByText(money(500000))).not.toBeInTheDocument()

    expect(await screen.findByText(t('wealthRoad.thisMonth.moveMasked'))).toBeInTheDocument()
    expect(screen.queryByText(wealthLeversFixture.move!.text)).not.toBeInTheDocument()
  })

  it('masks the idle-cash and cut_top lever amounts, which come from the user\'s own data', async () => {
    localStorage.setItem('privacyMode', 'true')

    renderWithProviders(<RoadTab />)

    await screen.findAllByText(PRIVACY_MASK)
    expect(screen.queryByText(t('wealthRoad.levers.idleCash', { amount: money(26000) }))).not.toBeInTheDocument()
  })

  it('does NOT mask the fixed +$500/+$1,000/+$2,000 lever increments — they are not the user\'s data', async () => {
    localStorage.setItem('privacyMode', 'true')

    renderWithProviders(<RoadTab />)

    expect(await screen.findByText(t('wealthRoad.levers.plusMonthly', { amount: money(500) }))).toBeInTheDocument()
  })
})

describe('RoadTab projection chart', () => {
  it('formats the target compactly ("$1M") in the takeaway title', async () => {
    renderWithProviders(<RoadTab />)

    expect(
      await screen.findByText(t('wealthRoad.chart.title', { amount: '$1M', label: 'Q4 2033' })),
    ).toBeInTheDocument()
  })
})

describe("RoadTab this month's move", () => {
  it('shows the investing-exceeds-saving note only when true', async () => {
    renderWithProviders(<RoadTab />)
    await screen.findByText(wealthLeversFixture.move!.text)
    expect(screen.queryByText(t('wealthRoad.thisMonth.investingExceedsSaving'))).not.toBeInTheDocument()
  })

  it('shows the note when investing exceeds saving', async () => {
    const levers: WealthLevers = { ...clone(wealthLeversFixture), investing_exceeds_saving: true }
    api.wealth.levers.mockResolvedValue(levers)

    renderWithProviders(<RoadTab />)

    expect(await screen.findByText(t('wealthRoad.thisMonth.investingExceedsSaving'))).toBeInTheDocument()
  })
})

describe('RoadTab chat context', () => {
  it('registers the route, goal target, projected label, coverage days and lever summary', async () => {
    renderWithProviders(<RoadTab />)

    await screen.findByText(t('wealthRoad.hero.coverage', { days: 94 }))

    const ctx = getPageChatContext()
    expect(ctx).toMatchObject({
      path: '/wealth',
      tab: 'road',
      goalTarget: 1000000,
      projectedLabel: 'Q1 2030',
      coverageDays: 94,
    })
    expect(ctx?.levers).toEqual(
      wealthProjectionFixture.levers.map((lever) => ({ key: lever.key, shiftMonths: lever.shift_months })),
    )
  })
})

describe('RoadTab failures', () => {
  it("a failing summary query doesn't blank the chart, levers or this month's move", async () => {
    api.wealth.summary.mockRejectedValue(new Error('boom'))

    renderWithProviders(<RoadTab />)

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    // The other sections still render from their own, unaffected queries.
    expect(await screen.findByText(wealthLeversFixture.move!.text)).toBeInTheDocument()
    expect(screen.getByText(t('wealthRoad.levers.idleCash', { amount: money(26000) }))).toBeInTheDocument()
  })

  it("a failing levers query doesn't blank the hero or the projection chart", async () => {
    api.wealth.levers.mockRejectedValue(new Error('boom'))

    renderWithProviders(<RoadTab />)

    expect(await screen.findByText(t('wealthRoad.hero.coverage', { days: 94 }))).toBeInTheDocument()
    // The idle-cash lever still shows, falling back to the projection's own amount.
    expect(screen.getByText(t('wealthRoad.levers.idleCash', { amount: money(20000) }))).toBeInTheDocument()
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })
})
