/**
 * Mirror of backend/app/schemas/wealth.py (fork, Wealth Cockpit). The lead owns
 * both files; change them together. Money is in `currency` (the workspace's
 * primary currency), rates are fractions, dates are `YYYY-MM-DD` strings.
 */

export const WEALTH_BUCKETS = [
  'cash',
  'taxable',
  'retirement_pretax',
  'retirement_roth',
  'retirement_hsa',
  'crypto',
  'property_other',
  'card_debt',
  'other_debt',
] as const
export type WealthBucket = (typeof WEALTH_BUCKETS)[number]

export type WealthPointFlag = 'estimated' | 'cash_flat' | 'no_holdings'
export type WealthSettingSource = 'default' | 'user'
export type WealthPrecision = 'quarter' | 'month'
export type WealthLeverKey =
  | 'idle_cash'
  | 'plus_500'
  | 'plus_1000'
  | 'plus_2000'
  | 'cut_top'
  | 'return_plus_1'
  | 'return_minus_1'

export interface WealthCoverage {
  from_date: string
  to_date: string
  days: number
  annualized: boolean
  insufficient_history: boolean
}

// --- settings ---------------------------------------------------------------

export interface WealthAccountAnnotation {
  bucket?: WealthBucket | null
  liquid?: boolean | null
  apr?: number | null
  fallback_key?: string | null
}

export interface WealthProjectionSettings {
  return_nominal: number
  withdrawal_rate: number
}

export interface WealthSettingsValues {
  schema_version: number
  goal_id: string | null
  projection: WealthProjectionSettings
  buffer_months: number
  income_category_ids: string[] | null
  one_off_overrides: Record<string, boolean>
  accounts: Record<string, WealthAccountAnnotation>
  spending_cap: number | null
}

export interface WealthAccountSettingRow {
  account_id: string | null
  name: string
  type: string | null
  institution: string | null
  default_bucket: WealthBucket | null
  bucket: WealthBucket
  liquid: boolean
  apr: number | null
  source: WealthSettingSource
  orphaned: boolean
  override_key: string
}

export interface WealthIncomeCategoryRow {
  id: string
  name: string
  group_name: string | null
  is_default: boolean
  selected: boolean
}

export interface WealthOneOffRow {
  transaction_id: string
  date: string
  description: string
  amount: number
  lane: 'income' | 'expenses' | 'invested'
  proposed: boolean
  selected: boolean
}

export interface WealthSettingsRead {
  values: WealthSettingsValues
  sources: Record<string, WealthSettingSource>
  accounts: WealthAccountSettingRow[]
  income_categories: WealthIncomeCategoryRow[]
  one_offs: WealthOneOffRow[]
}

export interface WealthSettingsUpdate {
  goal_id?: string | null
  clear_goal?: boolean
  projection?: WealthProjectionSettings
  buffer_months?: number
  income_category_ids?: string[]
  reset_income_categories?: boolean
  one_off_overrides?: Record<string, boolean | null>
  accounts?: Record<string, WealthAccountAnnotation | null>
  spending_cap?: number
  clear_spending_cap?: boolean
}

// --- summary ----------------------------------------------------------------

export interface WealthGoalProgress {
  id: string
  name: string
  target: number
  target_date: string | null
  current: number
  pct: number
}

export interface WealthSummary {
  as_of: string
  currency: string
  net_worth: number
  liquid: number
  goal: WealthGoalProgress | null
  projected_date: string | null
  projected_label: string | null
  precision: WealthPrecision
  coverage: WealthCoverage
  savings_rate_3m: number | null
  cash_flow_typical_monthly: number | null
  idle_cash: number
  cash_cushion_months: number | null
}

// --- net worth history ------------------------------------------------------

export interface WealthNetWorthPoint {
  date: string
  total: number
  liquid: number
  buckets: Record<WealthBucket, number>
  flags: WealthPointFlag[]
}

export interface WealthNetWorthHistory {
  currency: string
  synced_from: string | null
  points: WealthNetWorthPoint[]
}

// --- savings rate -----------------------------------------------------------

export interface WealthSavingsMonth {
  month: string
  income: number
  expenses: number
  direct: number
  rate: number | null
  cash_rate: number | null
  rolling_3m: number | null
  rolling_12m: number | null
  in_coverage: boolean
  partial: boolean
}

export interface WealthSavingsRateSeries {
  currency: string
  months: WealthSavingsMonth[]
}

// --- projection -------------------------------------------------------------

export interface WealthProjectionPathPoint {
  month: string
  balance: number
}

export interface WealthProjectionScenario {
  annual_return: number
  months_to_target: number | null
  date: string | null
  label: string | null
  path: WealthProjectionPathPoint[]
}

export interface WealthProjectionInputs {
  invested: number
  non_invested: number
  target_total: number
  target_invested: number
  monthly_contribution: number
  typical_annual_saving: number
  nominal: boolean
  coverage: WealthCoverage
}

export interface WealthLeverResult {
  key: WealthLeverKey
  amount: number | null
  months_to_target: number | null
  date: string | null
  label: string | null
  shift_months: number | null
}

export interface WealthFiFigures {
  fi_number_real: number | null
  annual_expenses_typical: number | null
  withdrawal_rate: number
}

export interface WealthProjection {
  currency: string
  as_of: string
  precision: WealthPrecision
  inputs: WealthProjectionInputs
  scenarios: Record<'low' | 'base' | 'high', WealthProjectionScenario>
  levers: WealthLeverResult[]
  fi: WealthFiFigures
}

// --- levers -----------------------------------------------------------------

export interface WealthCashFlow {
  typical_monthly: number | null
  average_monthly: number | null
  months: number
}

export interface WealthTopControllable {
  category: string
  monthly: number
  band_cut: number | null
}

export interface WealthThisMonthMove {
  rule: string
  text: string
  amount: number | null
}

export interface WealthLevers {
  currency: string
  as_of: string
  cash: number
  buffer: number
  idle_cash: number
  cash_cushion_months: number | null
  cash_flow: WealthCashFlow
  top_controllable: WealthTopControllable | null
  move: WealthThisMonthMove | null
  investing_exceeds_saving: boolean
}

// --- R2: spend smarter --------------------------------------------------------

export type WealthPaceStatus = 'under' | 'on_pace' | 'over' | 'no_budget'
export type WealthUpcomingKind = 'recurring' | 'card_bill' | 'installment' | 'pending'

export interface WealthBudgetSuggestion {
  category_id: string
  category: string
  group_name: string | null
  monthly_median: number
  months_used: number
  existing_budget: number | null
}

export interface WealthBudgetSuggestions {
  currency: string
  months: string[]
  suggestions: WealthBudgetSuggestion[]
  total_suggested: number
  typical_monthly_spending: number | null
}

export interface WealthApplyBudgetSuggestions {
  category_ids: string[]
}

export interface WealthApplyBudgetSuggestionsResult {
  created: number
  skipped: number
}

export interface WealthCapPace {
  cap: number
  spent: number
  expected_to_date: number
  projected_month_end: number
  status: WealthPaceStatus
  left_to_spend: number
  left_per_day: number | null
}

export interface WealthBudgetPace {
  currency: string
  month: string
  days_elapsed: number
  days_in_month: number
  cap: WealthCapPace | null
  spent: number
}

export interface WealthUpcomingCharge {
  date: string
  description: string
  amount: number
  kind: WealthUpcomingKind
  account: string | null
  category: string | null
  recurring_id: string | null
  /** False for a card purchase: it reaches cash only through the card's bill. */
  hits_cash: boolean
}

export interface WealthUpcoming {
  currency: string
  days: number
  items: WealthUpcomingCharge[]
  total: number
}

export interface WealthCashForecastPoint {
  date: string
  balance: number
  inflow: number
  outflow: number
}

export interface WealthCashForecast {
  currency: string
  days: number
  start_cash: number
  buffer: number
  points: WealthCashForecastPoint[]
  low_point: WealthCashForecastPoint | null
  first_negative_date: string | null
  first_below_buffer_date: string | null
}

export interface WealthSubscription {
  merchant: string
  monthly: number
  annual: number
  months_seen: number
  last_charge: string | null
  recurring_id: string | null
}

export interface WealthRecurringSuggestion {
  merchant: string
  monthly: number
  months: number
  create_payload: Record<string, unknown>
}

export interface WealthSubscriptionAudit {
  currency: string
  subscriptions: WealthSubscription[]
  monthly_total: number
  annual_total: number
  suggestions: WealthRecurringSuggestion[]
}

