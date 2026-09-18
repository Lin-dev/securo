import { formatAmountInput, parseAmountInput } from '@/lib/format'
import type { CategorySplitLineInput, Transaction } from '@/types'

/** A row that has been split into category lines. It is hidden from totals
 * (is_ignored) and its lines carry the money. */
export function isSplitParent(tx: Pick<Transaction, 'split_count'>): boolean {
  return (tx.split_count ?? 0) > 0
}

/** One category line of a split transaction. */
export function isSplitChild(tx: Pick<Transaction, 'parent_transaction_id'>): boolean {
  return !!tx.parent_transaction_id
}

/** Whether the split editor may be offered for an existing transaction.
 * Mirrors the backend's guards so the dialog never offers a split the API
 * would refuse. Parents pass too: the editor opens in "update" mode. */
export function canSplitByCategory(tx: Transaction | null | undefined): boolean {
  if (!tx) return false
  if (tx.virtual || tx.is_shared) return false
  if (isSplitChild(tx)) return false
  if (tx.status !== 'posted') return false
  if (tx.transfer_pair_id) return false
  if (tx.installment_series_id != null || tx.installment_number != null) return false
  if (tx.source === 'opening_balance' || tx.source === 'settlement') return false
  if ((tx.splits?.length ?? 0) > 0) return false
  return true
}

/** One line as the editor holds it: text fields, because amounts are typed
 * under the display locale's separators. `percent` and `remainder` are only
 * used by the rule editor's template mode. */
export interface CategorySplitEditorLine {
  key: string
  category_id: string
  amount: string
  percent: string
  remainder: boolean
  notes: string
}

let lineSequence = 0

export function newEditorLine(partial: Partial<CategorySplitEditorLine> = {}): CategorySplitEditorLine {
  lineSequence += 1
  return {
    key: `split-line-${lineSequence}`,
    category_id: '',
    amount: '',
    percent: '',
    remainder: false,
    notes: '',
    ...partial,
  }
}

export function defaultEditorLines(): CategorySplitEditorLine[] {
  return [newEditorLine(), newEditorLine()]
}

/** Same tolerance the group-split section uses: cents, never floating noise. */
export const SPLIT_EPSILON = 0.005

export function roundCents(value: number): number {
  return Math.round(value * 100) / 100
}

export function sumLines(lines: CategorySplitEditorLine[], locale: string): number {
  return roundCents(
    lines.reduce((sum, line) => sum + (parseAmountInput(line.amount, locale) ?? 0), 0),
  )
}

/** What is left to allocate: positive when the lines fall short, negative
 * when they overshoot. */
export function splitRemainder(target: number, lines: CategorySplitEditorLine[], locale: string): number {
  return roundCents(Math.abs(target) - sumLines(lines, locale))
}

/** Equal cents per line, the last line taking the rounding residue. */
export function splitEvenly(target: number, count: number): number[] {
  if (count <= 0) return []
  const totalCents = Math.round(Math.abs(target) * 100)
  const per = Math.floor(totalCents / count)
  const amounts = Array.from({ length: count }, () => per)
  amounts[count - 1] = totalCents - per * (count - 1)
  return amounts.map((cents) => cents / 100)
}

export type CategorySplitError = 'needTwoLines' | 'needCategory' | 'needAmount' | 'unbalanced'

export function validateCategorySplitLines(
  target: number,
  lines: CategorySplitEditorLine[],
  locale: string,
): { ok: boolean; errors: CategorySplitError[] } {
  const errors: CategorySplitError[] = []
  if (lines.length < 2) errors.push('needTwoLines')
  if (lines.some((line) => !line.category_id)) errors.push('needCategory')
  const amounts = lines.map((line) => parseAmountInput(line.amount, locale))
  if (amounts.some((amount) => amount == null || amount <= 0)) errors.push('needAmount')
  if (Math.abs(splitRemainder(target, lines, locale)) >= SPLIT_EPSILON) errors.push('unbalanced')
  return { ok: errors.length === 0, errors }
}

export function toSplitLinesPayload(
  lines: CategorySplitEditorLine[],
  locale: string,
): CategorySplitLineInput[] {
  return lines.map((line) => {
    const notes = line.notes.trim()
    return {
      category_id: line.category_id || null,
      amount: roundCents(parseAmountInput(line.amount, locale) ?? 0),
      ...(notes ? { notes } : {}),
    }
  })
}

/** Editor lines for a parent's existing lines, in the order they were entered. */
export function linesFromChildren(children: Transaction[], locale: string): CategorySplitEditorLine[] {
  return children.map((child) =>
    newEditorLine({
      category_id: child.category_id ?? '',
      amount: formatAmountInput(Math.abs(Number(child.amount)), locale),
      notes: child.notes ?? '',
    }),
  )
}

export interface SplitRole {
  isSplitChild: boolean
  isSplitParent: boolean
}

const CHILD_EDITABLE_FIELDS = new Set(['description', 'category_id', 'payee_id', 'notes'])
const PARENT_LOCKED_FIELDS = new Set([
  'amount', 'type', 'currency', 'account_id', 'status', 'is_ignored',
  'amount_primary', 'fx_rate_used', 'splits',
])

/** Trim a save payload to what the backend accepts for a split line or a
 * split parent. A line only carries its own bookkeeping; a parent's money
 * and identity are frozen while its lines exist. */
export function stripForSplitRole<T extends object>(payload: T, role: SplitRole): Partial<T> {
  const entries = Object.entries(payload as Record<string, unknown>)
  if (role.isSplitChild) {
    return Object.fromEntries(entries.filter(([key]) => CHILD_EDITABLE_FIELDS.has(key))) as Partial<T>
  }
  if (role.isSplitParent) {
    return Object.fromEntries(entries.filter(([key]) => !PARENT_LOCKED_FIELDS.has(key))) as Partial<T>
  }
  return payload
}
