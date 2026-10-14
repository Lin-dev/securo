/**
 * Typed fixtures for the wealth contracts (fork). Invented round numbers, not
 * anyone's real finances. Typed against src/types/wealth.ts, so a contract
 * change that breaks a consumer fails the typecheck here first.
 */
import type {
  WealthBucket,
  WealthLevers,
  WealthNetWorthHistory,
  WealthProjection,
  WealthSavingsRateSeries,
  WealthSettingsRead,
  WealthSummary,
} from '@/types/wealth'

const coverage = {
  from_date: '2026-07-13',
  to_date: '2026-10-14',
  days: 94,
  annualized: true,
  insufficient_history: false,
}

const buckets = (over: Partial<Record<WealthBucket, number>>): Record<WealthBucket, number> => ({
  cash: 0,
  taxable: 0,
  retirement_pretax: 0,
  retirement_roth: 0,
  retirement_hsa: 0,
  crypto: 0,
  property_other: 0,
  card_debt: 0,
  other_debt: 0,
  ...over,
})

export const wealthSummaryFixture: WealthSummary = {
  as_of: '2026-10-14',
  currency: 'USD',
  net_worth: 500000,
  liquid: 200000,
  goal: { id: 'goal-1', name: 'Millionaire', target: 1000000, target_date: '2030-12-31', current: 500000, pct: 0.5 },
  projected_date: '2030-03-01',
  projected_label: 'Q1 2030',
  precision: 'quarter',
  coverage,
  savings_rate_3m: 0.25,
  cash_flow_typical_monthly: 1500,
  idle_cash: 20000,
  cash_cushion_months: 6,
}

export const wealthNetWorthHistoryFixture: WealthNetWorthHistory = {
  currency: 'USD',
  synced_from: '2026-09-13',
  points: [
    {
      date: '2026-09-30',
      total: 490000,
      liquid: 195000,
      buckets: buckets({ cash: 60000, taxable: 140000, retirement_pretax: 240000, retirement_roth: 50000, crypto: 5000, card_debt: -5000 }),
      flags: [],
    },
    {
      date: '2026-10-14',
      total: 500000,
      liquid: 200000,
      buckets: buckets({ cash: 62000, taxable: 143000, retirement_pretax: 245000, retirement_roth: 50000, crypto: 5000, card_debt: -5000 }),
      flags: ['cash_flat'],
    },
  ],
}

export const wealthSavingsRateFixture: WealthSavingsRateSeries = {
  currency: 'USD',
  months: [
    { month: '2026-08-01', income: 15000, expenses: 11000, direct: 2000, rate: 0.353, cash_rate: 0.267, rolling_3m: null, rolling_12m: null, in_coverage: true, partial: false },
    { month: '2026-09-01', income: 15000, expenses: 12000, direct: 2000, rate: 0.294, cash_rate: 0.2, rolling_3m: null, rolling_12m: null, in_coverage: true, partial: false },
    { month: '2026-10-01', income: 7000, expenses: 6000, direct: 1000, rate: 0.25, cash_rate: 0.143, rolling_3m: 0.31, rolling_12m: null, in_coverage: true, partial: true },
  ],
}

const path = (start: number, step: number) =>
  Array.from({ length: 4 }, (_, i) => ({ month: `2027-0${i + 1}-01`, balance: start + step * i }))

export const wealthProjectionFixture: WealthProjection = {
  currency: 'USD',
  as_of: '2026-10-14',
  precision: 'quarter',
  inputs: {
    invested: 400000,
    non_invested: 100000,
    target_total: 1000000,
    target_invested: 900000,
    monthly_contribution: 3000,
    typical_annual_saving: 36000,
    nominal: true,
    coverage,
  },
  scenarios: {
    low: { annual_return: 0.03, months_to_target: 120, date: '2036-10-01', label: 'Q4 2036', path: path(400000, 4000) },
    base: { annual_return: 0.06, months_to_target: 84, date: '2033-10-01', label: 'Q4 2033', path: path(400000, 5000) },
    high: { annual_return: 0.09, months_to_target: 66, date: '2032-04-01', label: 'Q2 2032', path: path(400000, 6000) },
  },
  levers: [
    { key: 'idle_cash', amount: 20000, months_to_target: 80, date: '2033-06-01', label: 'Q2 2033', shift_months: -4 },
    { key: 'plus_500', amount: 500, months_to_target: 82, date: '2033-08-01', label: 'Q3 2033', shift_months: -2 },
    { key: 'return_minus_1', amount: null, months_to_target: 90, date: '2034-04-01', label: 'Q2 2034', shift_months: 6 },
  ],
  fi: { fi_number_real: 3600000, annual_expenses_typical: 144000, withdrawal_rate: 0.04 },
}

export const wealthLeversFixture: WealthLevers = {
  currency: 'USD',
  as_of: '2026-10-14',
  cash: 62000,
  buffer: 36000,
  idle_cash: 26000,
  cash_cushion_months: 5.2,
  cash_flow: { typical_monthly: 1500, average_monthly: -400, months: 2 },
  top_controllable: { category: 'Dining out', monthly: 1200, band_cut: 600 },
  move: { rule: 'idle_cash', text: 'Move 26,000.00 USD of idle cash into your brokerage.', amount: 26000 },
  investing_exceeds_saving: false,
}

export const wealthSettingsFixture: WealthSettingsRead = {
  values: {
    schema_version: 1,
    goal_id: 'goal-1',
    projection: { return_nominal: 0.06, withdrawal_rate: 0.04 },
    buffer_months: 3,
    income_category_ids: null,
    one_off_overrides: {},
    accounts: {},
  },
  sources: { goal_id: 'user', projection: 'default', buffer_months: 'default', income_category_ids: 'default', one_off_overrides: 'default', accounts: 'default' },
  accounts: [
    { account_id: 'acc-1', name: 'Checking', type: 'checking', institution: 'Bank', default_bucket: 'cash', bucket: 'cash', liquid: true, apr: null, source: 'default', orphaned: false, override_key: 'acc-1' },
    { account_id: 'acc-2', name: 'Roth IRA', type: 'investment', institution: 'Broker', default_bucket: 'retirement_roth', bucket: 'retirement_roth', liquid: false, apr: null, source: 'default', orphaned: false, override_key: 'acc-2' },
  ],
  income_categories: [{ id: 'cat-1', name: 'Salary', group_name: 'Income', is_default: true, selected: true }],
  one_offs: [{ transaction_id: 'tx-1', date: '2026-07-20', description: 'Lump deposit', amount: 9900, lane: 'invested', proposed: true, selected: true }],
}
