import type { TransactionsSummary } from '@/types'

/**
 * The five figures of the transactions summary line, by API payload key. Each
 * doubles as a `summary_scope` for the list endpoint, which then returns
 * exactly the rows behind that figure (the backend builds both from the same
 * predicate, so the rows always sum to the number).
 */
export const SUMMARY_SCOPES = ['income', 'expense', 'net', 'excluded', 'invested'] as const
export type SummaryScope = (typeof SUMMARY_SCOPES)[number]

/** URL search param and API query param share the name. */
export const SUMMARY_SCOPE_PARAM = 'summary_scope'

export function parseSummaryScope(raw: string | null | undefined): SummaryScope | null {
  return raw && (SUMMARY_SCOPES as readonly string[]).includes(raw) ? (raw as SummaryScope) : null
}

/** Single-select: clicking the active figure clears it, any other one switches. */
export function toggleSummaryScope(current: SummaryScope | null, clicked: SummaryScope): SummaryScope | null {
  return current === clicked ? null : clicked
}

const LABEL_KEYS: Record<SummaryScope, string> = {
  income: 'transactions.summaryIncome',
  expense: 'transactions.summaryExpenses',
  net: 'transactions.summaryNet',
  excluded: 'transactions.summaryExcluded',
  invested: 'transactions.summaryInvested',
}

export function summaryScopeLabelKey(scope: SummaryScope): string {
  return LABEL_KEYS[scope]
}

/** The figure for a scope; `invested` is optional in older payloads. */
export function summaryScopeValue(
  summary: Pick<TransactionsSummary, 'income' | 'expense' | 'net' | 'excluded' | 'invested'>,
  scope: SummaryScope,
): number {
  return scope === 'invested' ? (summary.invested ?? 0) : summary[scope]
}
