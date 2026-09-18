import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Split } from 'lucide-react'
import { toast } from 'sonner'

import { CategorySplitLinesEditor } from '@/components/category-split-lines-editor'
import { DeleteConfirmationDialog } from '@/components/delete-confirmation-dialog'
import { Button } from '@/components/ui/button'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { transactions as transactionsApi } from '@/lib/api'
import { extractApiError } from '@/lib/api-errors'
import { formatAmountInput, formatCurrency } from '@/lib/format'
import { invalidateFinancialQueries } from '@/lib/invalidate-queries'
import {
  defaultEditorLines,
  isSplitParent,
  linesFromChildren,
  splitEvenly,
  splitRemainder,
  toSplitLinesPayload,
  validateCategorySplitLines,
  type CategorySplitEditorLine,
  type CategorySplitError,
} from '@/lib/transaction-split-utils'
import type { Category, CategoryGroup, Transaction } from '@/types'

const ERROR_KEYS: Record<CategorySplitError, string> = {
  needTwoLines: 'transactions.splitNeedTwoLines',
  needCategory: 'transactions.splitNeedCategory',
  needAmount: 'transactions.splitNeedAmount',
  unbalanced: 'transactions.splitUnbalanced',
}

/**
 * Split one transaction across categories.
 *
 * Saves through its own endpoint rather than the dialog's PATCH, like the
 * ignore toggle: the split is a separate operation with its own validation,
 * and the dialog is mounted from three pages that would otherwise each need
 * the wiring. While a split is being drafted the dialog's main Save is held
 * back (`onDraftingChange`), and a successful split or unsplit closes the
 * dialog through `onDone` so the list re-reads the parent and its lines.
 */
export function TransactionCategorySplitSection({
  transaction,
  categories,
  categoryGroups,
  disabled = false,
  onDraftingChange,
  onDone,
}: {
  transaction: Transaction
  categories: Category[]
  categoryGroups: CategoryGroup[]
  disabled?: boolean
  onDraftingChange: (drafting: boolean) => void
  onDone: () => void
}) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const queryClient = useQueryClient()
  const isParent = isSplitParent(transaction)
  const target = Math.abs(Number(transaction.amount))
  const currency = transaction.currency

  const [enabled, setEnabled] = useState(isParent)
  // The rows the user has edited. Until then a parent shows its stored
  // lines and a fresh split shows two blank rows, so no effect has to copy
  // server data into state.
  const [editedLines, setEditedLines] = useState<CategorySplitEditorLine[] | null>(null)
  const [blankLines] = useState<CategorySplitEditorLine[]>(defaultEditorLines)
  const [confirmRemove, setConfirmRemove] = useState(false)

  const { data: existingLines } = useQuery({
    queryKey: ['transactions', transaction.id, 'split-lines'],
    queryFn: () => transactionsApi.splitLines(transaction.id),
    enabled: isParent,
  })

  const storedLines = useMemo(
    () => (isParent && existingLines && existingLines.length > 0 ? linesFromChildren(existingLines, locale) : null),
    [isParent, existingLines, locale],
  )
  const lines = editedLines ?? storedLines ?? blankLines
  const dirty = editedLines !== null

  const validation = useMemo(
    () => validateCategorySplitLines(target, lines, locale),
    [target, lines, locale],
  )
  const remaining = useMemo(() => splitRemainder(target, lines, locale), [target, lines, locale])
  const balanced = !validation.errors.includes('unbalanced')
  const firstError = validation.errors[0]

  const drafting = enabled && (!isParent || dirty)
  useEffect(() => {
    onDraftingChange(drafting)
    return () => onDraftingChange(false)
  }, [drafting, onDraftingChange])

  const splitMutation = useMutation({
    mutationFn: () => transactionsApi.splitByCategory(transaction.id, toSplitLinesPayload(lines, locale)),
    onSuccess: (result) => {
      invalidateFinancialQueries(queryClient)
      toast.success(t('transactions.splitSuccess', { count: result.children.length }))
      onDone()
    },
    onError: (error) => toast.error(extractApiError(error)),
  })

  const removeMutation = useMutation({
    mutationFn: () => transactionsApi.removeCategorySplit(transaction.id),
    onSuccess: () => {
      invalidateFinancialQueries(queryClient)
      setConfirmRemove(false)
      toast.success(t('transactions.splitRemoved'))
      onDone()
    },
    onError: (error) => toast.error(extractApiError(error)),
  })

  const busy = disabled || splitMutation.isPending || removeMutation.isPending

  const handleLinesChange = (next: CategorySplitEditorLine[]) => {
    setEditedLines(next)
  }

  const handleSplitEvenly = () => {
    const amounts = splitEvenly(target, lines.length)
    handleLinesChange(lines.map((line, i) => ({ ...line, amount: formatAmountInput(amounts[i] ?? 0, locale) })))
  }

  return (
    <div className="space-y-3 pt-2 border-t border-border">
      {isParent ? (
        <div className="text-sm font-medium inline-flex items-center gap-2">
          <Split size={14} />
          {t('transactions.splitByCategory')}
        </div>
      ) : (
        <label className="text-sm font-medium inline-flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            disabled={busy}
            className="h-4 w-4 rounded border-border accent-primary"
          />
          <Split size={14} />
          {t('transactions.splitByCategory')}
        </label>
      )}

      {enabled && (
        <div className="space-y-3 pl-6">
          <p className="text-xs text-muted-foreground">
            {isParent ? t('transactions.splitParentInfo') : t('transactions.splitByCategoryHint')}
          </p>
          <CategorySplitLinesEditor
            lines={lines}
            onChange={handleLinesChange}
            categories={categories}
            categoryGroups={categoryGroups}
            mode="amount"
            disabled={busy}
          />
          <div className="flex items-center justify-between gap-2 text-xs">
            <button
              type="button"
              className="text-primary hover:underline font-medium disabled:opacity-50"
              onClick={handleSplitEvenly}
              disabled={busy}
            >
              {t('transactions.splitEvenly')}
            </button>
            <span className={`tabular-nums ${balanced ? 'text-emerald-600' : 'text-amber-600'}`}>
              {balanced
                ? t('transactions.splitBalanced')
                : t('transactions.splitRemaining', { amount: formatCurrency(remaining, currency, locale) })}
            </span>
          </div>
          {firstError && (
            <p className="text-xs text-amber-600">
              {t(ERROR_KEYS[firstError], { amount: formatCurrency(target, currency, locale) })}
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              size="sm"
              onClick={() => splitMutation.mutate()}
              disabled={busy || !validation.ok || (isParent && !dirty)}
            >
              {splitMutation.isPending
                ? t('common.saving')
                : isParent
                  ? t('transactions.splitUpdate')
                  : t('transactions.splitSave')}
            </Button>
            {isParent && (
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="text-rose-600 hover:text-rose-700"
                onClick={() => setConfirmRemove(true)}
                disabled={busy}
              >
                {t('transactions.splitRemove')}
              </Button>
            )}
          </div>
        </div>
      )}

      <DeleteConfirmationDialog
        open={confirmRemove}
        title={t('transactions.splitRemoveTitle')}
        description={t('transactions.splitRemoveConfirm')}
        isPending={removeMutation.isPending}
        onClose={() => setConfirmRemove(false)}
        onConfirm={() => removeMutation.mutate()}
      />
    </div>
  )
}
