import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'

import { SettingsTab } from '@/components/wealth/settings/settings-tab'
import { wealthSettingsFixture } from '@/test/fixtures/wealth'
import { renderWithProviders, t } from '@/test/utils'
import type { Goal } from '@/types'
import type { WealthSettingsRead } from '@/types/wealth'

const api = vi.hoisted(() => ({
  wealth: { settings: vi.fn(), updateSettings: vi.fn() },
  goals: { list: vi.fn() },
}))

vi.mock('@/lib/api', () => api)

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }),
}))

// The real hook (not mocked): it reads `localStorage.privacyMode`, which the
// shared test setup clears after every test, so every test here is "off"
// unless it opts in.
const PRIVACY_MASK = '•••••'

function goal(overrides: Partial<Goal>): Goal {
  return {
    id: 'goal-1',
    user_id: 'user-1',
    name: 'Millionaire',
    target_amount: 1000000,
    current_amount: 500000,
    currency: 'USD',
    target_amount_primary: null,
    current_amount_primary: null,
    target_date: '2030-12-31',
    tracking_type: 'net_worth',
    account_id: null,
    asset_id: null,
    asset_group_id: null,
    status: 'active',
    icon: null,
    color: null,
    position: 0,
    metadata_json: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    percentage: 50,
    monthly_contribution: 3000,
    on_track: 'on_track',
    account_name: null,
    asset_name: null,
    asset_group_name: null,
    ...overrides,
  }
}

function cloneFixture(): WealthSettingsRead {
  return JSON.parse(JSON.stringify(wealthSettingsFixture)) as WealthSettingsRead
}

beforeEach(() => {
  vi.clearAllMocks()
  api.wealth.settings.mockResolvedValue(cloneFixture())
  api.wealth.updateSettings.mockResolvedValue(cloneFixture())
  api.goals.list.mockResolvedValue([goal({})])
})

describe('SettingsTab', () => {
  it('renders the fixture', async () => {
    renderWithProviders(<SettingsTab />)

    expect(await screen.findByText('Checking')).toBeInTheDocument()
    expect(screen.getByText('Roth IRA')).toBeInTheDocument()
    expect(screen.getByText('Salary')).toBeInTheDocument()
    expect(screen.getByText('Lump deposit')).toBeInTheDocument()
    expect(
      await screen.findByRole('combobox', { name: t('wealthSettings.milestone.label') }),
    ).toBeInTheDocument()
  })

  it('changing a bucket sends the right PUT body', async () => {
    const { user } = renderWithProviders(<SettingsTab />)
    await screen.findByText('Checking')

    await user.click(screen.getByRole('combobox', { name: 'Bucket for Checking' }))
    await user.click(await screen.findByRole('option', { name: 'Taxable' }))

    await waitFor(() =>
      expect(api.wealth.updateSettings).toHaveBeenCalledWith({
        accounts: { 'acc-1': { bucket: 'taxable', liquid: true, apr: null } },
      }),
    )
  })

  it('reset sends null', async () => {
    const fixture = cloneFixture()
    fixture.accounts[0] = { ...fixture.accounts[0], source: 'user' }
    api.wealth.settings.mockResolvedValue(fixture)

    const { user } = renderWithProviders(<SettingsTab />)
    await screen.findByText('Checking')

    await user.click(screen.getByRole('button', { name: t('wealthSettings.accounts.reset') }))

    await waitFor(() =>
      expect(api.wealth.updateSettings).toHaveBeenCalledWith({ accounts: { 'acc-1': null } }),
    )
  })

  it('shows a validation message and sends nothing for an invalid return %', async () => {
    const { user } = renderWithProviders(<SettingsTab />)
    await screen.findByText('Checking')

    const input = screen.getByLabelText(t('wealthSettings.assumptions.returnLabel'))
    await user.clear(input)
    await user.type(input, '20')
    await user.tab()

    expect(await screen.findByText(t('wealthSettings.assumptions.returnError'))).toBeInTheDocument()
    expect(api.wealth.updateSettings).not.toHaveBeenCalled()
  })

  it('"Use defaults" sends reset_income_categories', async () => {
    const { user } = renderWithProviders(<SettingsTab />)
    await screen.findByText('Salary')

    await user.click(screen.getByRole('button', { name: t('wealthSettings.income.useDefaults') }))

    await waitFor(() =>
      expect(api.wealth.updateSettings).toHaveBeenCalledWith({ reset_income_categories: true }),
    )
  })

  it('a one-off switch sends the override', async () => {
    const { user } = renderWithProviders(<SettingsTab />)
    await screen.findByText('Lump deposit')

    const toggleName = t('wealthSettings.oneOffs.toggleLabel', { description: 'Lump deposit' })
    await user.click(screen.getByRole('switch', { name: toggleName }))

    await waitFor(() =>
      expect(api.wealth.updateSettings).toHaveBeenCalledWith({ one_off_overrides: { 'tx-1': false } }),
    )
  })

  it("an orphaned row's Remove button sends null", async () => {
    const fixture = cloneFixture()
    fixture.accounts.push({
      account_id: null,
      name: 'Old Card',
      type: null,
      institution: null,
      default_bucket: null,
      bucket: 'card_debt',
      liquid: false,
      apr: 24.99,
      source: 'user',
      orphaned: true,
      override_key: 'orphan-1',
    })
    api.wealth.settings.mockResolvedValue(fixture)

    const { user } = renderWithProviders(<SettingsTab />)
    await screen.findByText('Old Card')

    await user.click(screen.getByRole('button', { name: t('common.remove') }))

    await waitFor(() =>
      expect(api.wealth.updateSettings).toHaveBeenCalledWith({ accounts: { 'orphan-1': null } }),
    )
  })

  it('masks one-off amounts in privacy mode', async () => {
    localStorage.setItem('privacyMode', 'true')

    renderWithProviders(<SettingsTab />)
    await screen.findByText('Lump deposit')

    expect(screen.getByText(PRIVACY_MASK)).toBeInTheDocument()
    expect(screen.queryByText('$9,900.00')).not.toBeInTheDocument()
  })

  it('shows the link to /goals when there is no net-worth goal', async () => {
    api.goals.list.mockResolvedValue([goal({ tracking_type: 'manual' })])

    renderWithProviders(<SettingsTab />)
    await screen.findByText('Checking')

    expect(screen.getByText(t('wealthSettings.milestone.empty'))).toBeInTheDocument()
    const link = await screen.findByRole('link', { name: t('wealthSettings.milestone.goToGoals') })
    expect(link).toHaveAttribute('href', '/goals')
  })
})
