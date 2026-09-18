import { X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { CategorySelect } from '@/components/category-select'
import { Input } from '@/components/ui/input'
import { newEditorLine, type CategorySplitEditorLine } from '@/lib/transaction-split-utils'
import type { Category, CategoryGroup } from '@/types'

type SplitLineUnit = 'amount' | 'percent' | 'remainder'

/** Which of the three template inputs a line is using. */
function lineUnit(line: CategorySplitEditorLine): SplitLineUnit {
  if (line.remainder) return 'remainder'
  if (line.percent !== '') return 'percent'
  return 'amount'
}

const SELECT_CLASS =
  'border border-border rounded-md px-2 py-1.5 text-sm bg-card text-foreground focus:outline-none focus-visible:ring-ring/30 focus-visible:ring-[2px] disabled:opacity-50'

/**
 * The line rows shared by the transaction split editor and the rule editor.
 *
 * `amount` mode takes absolute amounts (plus notes) for one transaction.
 * `template` mode is a rule's recipe: each line takes an amount, a percent
 * of the matched transaction, or whatever is left after the others. Amount
 * fields are plain text inputs read through the display locale, like the
 * dialog's own amount field, so a comma decimal is not mistaken for a
 * thousands separator.
 */
export function CategorySplitLinesEditor({
  lines,
  onChange,
  categories,
  categoryGroups,
  mode,
  disabled = false,
  minLines = 2,
  currentCategories = [],
}: {
  lines: CategorySplitEditorLine[]
  onChange: (lines: CategorySplitEditorLine[]) => void
  categories: Category[]
  categoryGroups: CategoryGroup[]
  mode: 'amount' | 'template'
  disabled?: boolean
  minLines?: number
  /** Categories no longer offered by the picker that a line may still point at. */
  currentCategories?: Category[]
}) {
  const { t } = useTranslation()

  const update = (index: number, patch: Partial<CategorySplitEditorLine>) => {
    onChange(lines.map((line, i) => (i === index ? { ...line, ...patch } : line)))
  }

  const setUnit = (index: number, unit: SplitLineUnit) => {
    if (unit === 'remainder') update(index, { amount: '', percent: '', remainder: true })
    else if (unit === 'percent') update(index, { amount: '', remainder: false, percent: lines[index].percent || '' })
    else update(index, { percent: '', remainder: false })
  }

  const remove = (index: number) => {
    onChange(lines.filter((_, i) => i !== index))
  }

  return (
    <div className="space-y-2">
      {lines.map((line, index) => {
        const unit = lineUnit(line)
        const otherHasRemainder = lines.some((other) => other !== line && other.remainder)
        const current =
          currentCategories.find((category) => category.id === line.category_id) ??
          categories.find((category) => category.id === line.category_id)
        return (
          <div key={line.key} className="flex flex-wrap items-center gap-2 sm:flex-nowrap">
            <div className="w-full min-w-0 sm:w-0 sm:flex-1">
              <CategorySelect
                value={line.category_id}
                onChange={(value) => update(index, { category_id: value })}
                categories={categories}
                groups={categoryGroups}
                currentCategory={current}
                placeholder={t('transactions.category')}
                disabled={disabled}
                className="bg-card w-full"
              />
            </div>
            {mode === 'amount' ? (
              <>
                <Input
                  type="text"
                  inputMode="decimal"
                  className="h-8 w-28 text-sm text-right tabular-nums bg-card"
                  value={line.amount}
                  onChange={(e) => update(index, { amount: e.target.value })}
                  placeholder="0.00"
                  disabled={disabled}
                  aria-label={t('transactions.amount')}
                />
                <Input
                  className="h-8 w-full min-w-0 text-sm bg-card sm:w-40"
                  value={line.notes}
                  onChange={(e) => update(index, { notes: e.target.value })}
                  placeholder={t('transactions.notesPlaceholder')}
                  disabled={disabled}
                  aria-label={t('transactions.notes')}
                />
              </>
            ) : (
              <>
                <select
                  className={`${SELECT_CLASS} h-8 w-32 shrink-0`}
                  value={unit}
                  onChange={(e) => setUnit(index, e.target.value as SplitLineUnit)}
                  disabled={disabled}
                  aria-label={t('rules.splitUnitAmount')}
                >
                  <option value="amount">{t('rules.splitUnitAmount')}</option>
                  <option value="percent">{t('rules.splitUnitPercent')}</option>
                  <option value="remainder" disabled={otherHasRemainder}>
                    {t('rules.splitRemainder')}
                  </option>
                </select>
                {unit === 'remainder' ? (
                  <span className="text-xs italic text-muted-foreground">{t('rules.splitRemainderHint')}</span>
                ) : (
                  <div className="flex items-center gap-1">
                    <Input
                      type="text"
                      inputMode="decimal"
                      className="h-8 w-24 text-sm text-right tabular-nums bg-card"
                      value={unit === 'percent' ? line.percent : line.amount}
                      onChange={(e) =>
                        update(index, unit === 'percent' ? { percent: e.target.value } : { amount: e.target.value })
                      }
                      placeholder={unit === 'percent' ? '50' : '0.00'}
                      disabled={disabled}
                      aria-label={unit === 'percent' ? t('rules.splitUnitPercent') : t('rules.splitUnitAmount')}
                    />
                    {unit === 'percent' && <span className="text-xs text-muted-foreground">%</span>}
                  </div>
                )}
              </>
            )}
            <button
              type="button"
              className="shrink-0 p-1 text-muted-foreground transition-colors hover:text-rose-500 disabled:opacity-40 disabled:hover:text-muted-foreground"
              onClick={() => remove(index)}
              disabled={disabled || lines.length <= minLines}
              aria-label={t('transactions.splitRemoveLine')}
              title={t('transactions.splitRemoveLine')}
            >
              <X size={13} />
            </button>
          </div>
        )
      })}
      <button
        type="button"
        className="text-xs text-primary hover:underline font-medium disabled:opacity-50"
        onClick={() => onChange([...lines, newEditorLine()])}
        disabled={disabled}
      >
        + {t('transactions.splitAddLine')}
      </button>
    </div>
  )
}
