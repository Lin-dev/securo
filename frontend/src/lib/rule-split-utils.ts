import { formatAmountInput, parseAmountInput } from '@/lib/format'
import { newEditorLine, type CategorySplitEditorLine } from '@/lib/transaction-split-utils'
import type { RuleSplitLine } from '@/types'

export const SPLIT_RULE_OP = 'split_categories'

/** A `split_categories` action value as the API stores it. */
export function isRuleSplitLines(value: unknown): value is RuleSplitLine[] {
  return (
    Array.isArray(value) &&
    value.every(
      (line) => !!line && typeof line === 'object' && typeof (line as RuleSplitLine).category_id === 'string',
    )
  )
}

/** A fresh action: one line to fill in, one taking whatever is left. */
export function defaultRuleSplitLines(): RuleSplitLine[] {
  return [{ category_id: '' }, { category_id: '', remainder: true }]
}

export function ruleLinesToEditor(lines: RuleSplitLine[], locale: string): CategorySplitEditorLine[] {
  return lines.map((line) =>
    newEditorLine({
      category_id: line.category_id ?? '',
      amount: line.amount != null ? formatAmountInput(line.amount, locale, 8) : '',
      percent: line.percent != null ? formatAmountInput(line.percent, locale, 8) : '',
      remainder: !!line.remainder,
    }),
  )
}

/** The stored shape of what the editor holds. Text that does not parse yet
 * (a trailing decimal separator while typing) is left out of the value, so
 * the draft stays incomplete rather than wrong until the field is finished. */
export function editorToRuleLines(lines: CategorySplitEditorLine[], locale: string): RuleSplitLine[] {
  return lines.map((line) => {
    const out: RuleSplitLine = { category_id: line.category_id }
    if (line.remainder) {
      out.remainder = true
    } else if (line.percent !== '') {
      const percent = parseAmountInput(line.percent, locale)
      if (percent != null) out.percent = percent
    } else if (line.amount !== '') {
      const amount = parseAmountInput(line.amount, locale)
      if (amount != null) out.amount = amount
    }
    return out
  })
}

export type RuleSplitError =
  | 'needTwoLines'
  | 'needCategory'
  | 'needValue'
  | 'tooManyRemainders'
  | 'percentOver'
  | 'percentShort'

/** Mirrors the backend's validation of a split action. */
export function validateRuleSplitLines(lines: RuleSplitLine[]): RuleSplitError[] {
  const errors: RuleSplitError[] = []
  if (lines.length < 2) errors.push('needTwoLines')
  if (lines.some((line) => !line.category_id)) errors.push('needCategory')
  let remainders = 0
  let fixed = 0
  let percentTotal = 0
  for (const line of lines) {
    const hasAmount = line.amount != null && line.amount > 0
    const hasPercent = line.percent != null && line.percent > 0 && line.percent <= 100
    const hasRemainder = !!line.remainder
    if (Number(hasAmount) + Number(hasPercent) + Number(hasRemainder) !== 1) {
      if (!errors.includes('needValue')) errors.push('needValue')
    }
    if (hasAmount) fixed += 1
    if (hasPercent) percentTotal += line.percent ?? 0
    if (hasRemainder) remainders += 1
  }
  if (remainders > 1) errors.push('tooManyRemainders')
  if (percentTotal > 100 + 1e-9) errors.push('percentOver')
  if (remainders === 0 && fixed === 0 && percentTotal < 100 - 1e-9) errors.push('percentShort')
  return errors
}

export function isValidRuleSplitLines(value: unknown): boolean {
  return isRuleSplitLines(value) && validateRuleSplitLines(value).length === 0
}
