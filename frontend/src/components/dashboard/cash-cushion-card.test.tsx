import { afterEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'

vi.mock('@/hooks/use-display-locale', () => ({ useDisplayLocale: () => 'en-US' }))

import { CashCushionCard } from '@/components/dashboard/cash-cushion-card'
import { renderWithProviders, t } from '@/test/utils'

afterEach(() => {
  localStorage.clear()
})

describe('CashCushionCard', () => {
  it('shows the months of cash cushion to one decimal', () => {
    renderWithProviders(
      <CashCushionCard cashCushionMonths={6} idleCash={0} currency="USD" isLoading={false} />,
    )

    expect(screen.getByText(t('dashboard.wealthCashCushionMonths', { months: '6.0' }))).toBeInTheDocument()
  })

  it('shows idle cash above the buffer only when it is above 0', () => {
    renderWithProviders(
      <CashCushionCard cashCushionMonths={5.2} idleCash={20000} currency="USD" isLoading={false} />,
    )

    expect(
      screen.getByText(t('dashboard.wealthIdleCashAboveBuffer', { amount: '$20,000.00' })),
    ).toBeInTheDocument()
  })

  it('omits the idle-cash line when there is none above the buffer', () => {
    renderWithProviders(
      <CashCushionCard cashCushionMonths={5.2} idleCash={0} currency="USD" isLoading={false} />,
    )

    expect(screen.queryByText(/Idle cash/)).not.toBeInTheDocument()
  })

  it('falls back to an "unknown" message when there is no cushion figure yet', () => {
    renderWithProviders(
      <CashCushionCard cashCushionMonths={null} idleCash={0} currency="USD" isLoading={false} />,
    )

    expect(screen.getByText(t('dashboard.wealthCashCushionUnknown'))).toBeInTheDocument()
  })

  it('masks both the months and the idle cash in privacy mode', () => {
    localStorage.setItem('privacyMode', 'true')

    renderWithProviders(
      <CashCushionCard cashCushionMonths={6} idleCash={20000} currency="USD" isLoading={false} />,
    )

    expect(screen.queryByText(t('dashboard.wealthCashCushionMonths', { months: '6.0' }))).not.toBeInTheDocument()
    expect(screen.queryByText('$20,000.00')).not.toBeInTheDocument()
    expect(screen.queryByText(/6\.0/)).not.toBeInTheDocument()
  })

  it('links to the wealth page and to the buffer settings', () => {
    renderWithProviders(
      <CashCushionCard cashCushionMonths={6} idleCash={0} currency="USD" isLoading={false} />,
    )

    expect(
      screen.getByRole('link', { name: new RegExp(t('dashboard.wealthCashCushionViewLink')) }),
    ).toHaveAttribute('href', '/wealth')
    expect(
      screen.getByRole('link', { name: new RegExp(t('dashboard.wealthCashCushionChangeBuffer')) }),
    ).toHaveAttribute('href', '/wealth?tab=settings')
  })
})
