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
    await screen.findByText(t('wealthSavings.title.rolling', { rate: '31%' }))

    // Hidden by default (it's neither in_coverage nor partial) -- the note
    // only appears once "Show earlier months" brings it into the chart.
    expect(screen.queryByText(t('wealthSavings.flags.outsideCoverage'))).not.toBeInTheDocument()
    await user.click(screen.getByRole('switch', { name: t('wealthSavings.showEarlierToggleLabel') }))
    await screen.findByText(t('wealthSavings.flags.outsideCoverage'))

    // The table always lists every month, regardless of that toggle.
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

  describe('pre-coverage stray months (R1 gate follow-up)', () => {
    function withStrayMonths(): WealthSavingsRateSeries {
      const withStray = series()
      withStray.months.unshift(
        { month: '2026-03-01', income: 50, expenses: 900, direct: 0, rate: -16, cash_rate: -16, rolling_3m: null, rolling_12m: null, in_coverage: false, partial: false },
        { month: '2026-04-01', income: 20, expenses: 400, direct: 0, rate: -19, cash_rate: -19, rolling_3m: null, rolling_12m: null, in_coverage: false, partial: false },
        { month: '2026-05-01', income: 80, expenses: 300, direct: 0, rate: -2.75, cash_rate: -2.75, rolling_3m: null, rolling_12m: null, in_coverage: false, partial: false },
      )
      return withStray
    }

    it('hides pre-coverage months from the chart by default', async () => {
      api.wealth.savingsRate.mockResolvedValue(withStrayMonths())
      const { container } = renderWithProviders(<SavingsTab />)
      await screen.findByText(t('wealthSavings.title.rolling', { rate: '31%' }))

      expect(within(container).queryByText('Mar 2026')).not.toBeInTheDocument()
      expect(container.querySelectorAll('.recharts-bar-rectangle')).toHaveLength(3)
    })

    it('"Show earlier months" reveals them, still greyed out', async () => {
      api.wealth.savingsRate.mockResolvedValue(withStrayMonths())
      const { container, user } = renderWithProviders(<SavingsTab />)
      await screen.findByText(t('wealthSavings.title.rolling', { rate: '31%' }))

      await user.click(screen.getByRole('switch', { name: t('wealthSavings.showEarlierToggleLabel') }))

      expect(within(container).getByText('Mar 2026')).toBeInTheDocument()
      expect(container.querySelectorAll('.recharts-bar-rectangle')).toHaveLength(6)
      expect(screen.getByText(t('wealthSavings.flags.outsideCoverage'))).toBeInTheDocument()
    })

    it('keeps the takeaway title and rolling line based on the visible months only', async () => {
      api.wealth.savingsRate.mockResolvedValue(withStrayMonths())
      renderWithProviders(<SavingsTab />)

      // The rolling rate (31%) comes from the in-coverage October row; the
      // stray months (which carry no rolling_3m of their own) must not
      // change the title even though they're part of the full months list.
      expect(await screen.findByText(t('wealthSavings.title.rolling', { rate: '31%' }))).toBeInTheDocument()
    })

    it("the table lists every month, including the ones hidden from the chart", async () => {
      api.wealth.savingsRate.mockResolvedValue(withStrayMonths())
      const { user } = renderWithProviders(<SavingsTab />)
      await screen.findByText(t('wealthSavings.title.rolling', { rate: '31%' }))

      await user.click(screen.getByRole('button', { name: t('charts.showTable') }))
      const table = within(screen.getByRole('table'))
      expect(table.getByText('Mar 2026', { exact: false })).toBeInTheDocument()
      expect(table.getByText('Apr 2026', { exact: false })).toBeInTheDocument()
      expect(table.getByText('May 2026', { exact: false })).toBeInTheDocument()
    })
  })

  describe('an extreme outlier month (R1 gate follow-up)', () => {
    function withOutlier(): WealthSavingsRateSeries {
      const outlier = series()
      outlier.months[0] = { ...outlier.months[0], rate: -2.34, cash_rate: -2.34 }
      return outlier
    }

    it('clamps the y-axis domain to ±100% (allowDataOverflow clips the bars layer at the plot edge, not at the outlier)', async () => {
      api.wealth.savingsRate.mockResolvedValue(withOutlier())
      const { container } = renderWithProviders(<SavingsTab />)
      await screen.findByText(t('wealthSavings.title.rolling', { rate: '31%' }))

      // `allowDataOverflow` (plan follow-up) makes recharts clip the bars'
      // layer to a <clipPath> sized to the plot area -- not to the raw,
      // unclamped -234% bar -- which is the DOM evidence that the domain
      // itself stayed clamped instead of stretching to fit the outlier.
      // The exact clamp math (`domainMin`/`domainMax`) is unit-tested in
      // savings-data.test.ts; jsdom doesn't lay out SVG, so pixel geometry
      // isn't asserted here.
      const barsLayer = container.querySelector('.recharts-bar-rectangles')
      const clipPathUrl = barsLayer?.getAttribute('clip-path')
      expect(clipPathUrl).toMatch(/^url\(#.+\)$/)

      const clipId = clipPathUrl!.replace(/^url\(#(.+)\)$/, '$1')
      const clipRect = container.querySelector(`#${clipId} rect`)
      expect(clipRect).not.toBeNull()
      // A plot-sized clip is at most a few hundred px tall; an axis that had
      // stretched to fit a -234% bar on this same chart would need a clip
      // several times taller.
      expect(Number(clipRect!.getAttribute('height'))).toBeLessThan(400)
    })

    it('marks the clipped bar with its true value and shows the clipped footnote', async () => {
      api.wealth.savingsRate.mockResolvedValue(withOutlier())
      const { container } = renderWithProviders(<SavingsTab />)
      await screen.findByText(t('wealthSavings.title.rolling', { rate: '31%' }))

      expect(screen.getByText(t('wealthSavings.flags.clipped'))).toBeInTheDocument()
      expect(within(container).getByText('-234%')).toBeInTheDocument()
    })

    it("does not clip or flag a month within ±100%", async () => {
      renderWithProviders(<SavingsTab />)
      await screen.findByText(t('wealthSavings.title.rolling', { rate: '31%' }))

      expect(screen.queryByText(t('wealthSavings.flags.clipped'))).not.toBeInTheDocument()
    })
  })
})
