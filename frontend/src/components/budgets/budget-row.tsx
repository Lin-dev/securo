import type { KeyboardEvent, MouseEvent, ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Pencil, Repeat, Trash2 } from 'lucide-react'

import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { BulletBar } from '@/components/charts/BulletBar'
import { formatMoney } from '@/lib/chart-format'
import type { Budget, BudgetVsActual } from '@/types'

import { PaceBadge } from './pace-badge'

export interface BudgetRowProps {
  budget: Budget
  comparison?: BudgetVsActual
  categoryName: string
  categoryIcon: ReactNode
  currency: string
  locale: string
  canWrite: boolean
  onEdit: () => void
  onDelete: () => void
  onNavigate: () => void
}

/**
 * One budget row (plan S7): a bullet bar of actual vs. budget, with the
 * current month's pace marker and badge when the comparison carries pace
 * fields, and the pre-existing projected/previous-month figures otherwise
 * shown only on the dashboard. Clicking the row (or pressing Enter on it)
 * drills into Transactions for this category and month; the edit/delete
 * controls stop that click from also firing.
 */
export function BudgetRow({
  budget,
  comparison,
  categoryName,
  categoryIcon,
  currency,
  locale,
  canWrite,
  onEdit,
  onDelete,
  onNavigate,
}: BudgetRowProps) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()

  const actual = comparison ? Number(comparison.actual_amount) : 0
  const projected = comparison?.projected_month_end != null
    ? Number(comparison.projected_month_end)
    : comparison
      ? Number(comparison.projected_amount)
      : null
  const prevMonth = comparison ? Number(comparison.prev_month_amount) : null
  const marker = comparison?.expected_to_date != null ? Number(comparison.expected_to_date) : undefined
  const leftToSpend = comparison?.left_to_spend != null ? Number(comparison.left_to_spend) : null
  const leftPerDay = comparison?.left_per_day != null ? Number(comparison.left_per_day) : null
  const paceStatus = comparison?.pace_status ?? null

  function handleKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    // Only act when the row itself has focus -- a bubbled Enter from the
    // edit/delete buttons inside it should trigger their own click, not navigate.
    if (e.key !== 'Enter' || e.target !== e.currentTarget) return
    e.preventDefault()
    onNavigate()
  }

  function stopRowClick(e: MouseEvent) {
    e.stopPropagation()
  }

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onNavigate}
      onKeyDown={handleKeyDown}
      aria-label={t('budgetsPace.row.viewTransactions', { category: categoryName })}
      className="px-4 sm:px-5 py-3.5 border-b border-border last:border-0 hover:bg-muted/50 transition-colors cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-inset"
    >
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="flex items-center gap-2 min-w-0">
          {categoryIcon}
          <span className="text-sm font-medium text-foreground truncate">{categoryName}</span>
          {budget.is_recurring && (
            <span title={t('budgets.recurringLabel')} className="text-muted-foreground shrink-0">
              <Repeat size={12} aria-hidden="true" />
            </span>
          )}
          {paceStatus && <PaceBadge status={paceStatus} className="shrink-0" />}
        </div>
        {canWrite && (
          <div className="flex items-center gap-1 shrink-0" onClick={stopRowClick}>
            <button
              type="button"
              className="p-1.5 rounded-md text-muted-foreground hover:text-primary hover:bg-primary/5 transition-colors"
              onClick={onEdit}
              aria-label={t('common.edit')}
              title={t('common.edit')}
            >
              <Pencil size={13} />
            </button>
            <button
              type="button"
              className="p-1.5 rounded-md text-muted-foreground hover:text-rose-500 hover:bg-rose-50 transition-colors"
              onClick={onDelete}
              aria-label={t('common.delete')}
              title={t('common.delete')}
            >
              <Trash2 size={13} />
            </button>
          </div>
        )}
      </div>

      <BulletBar
        value={actual}
        target={budget.amount}
        marker={marker}
        markerLabel={t('budgetsPace.marker')}
        currency={currency}
      />

      <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {projected != null && (
          <span>{t('budgetsPace.projected', { amount: mask(formatMoney(projected, currency, { locale })) })}</span>
        )}
        {prevMonth != null && (
          <span>{t('budgetsPace.lastMonth', { amount: mask(formatMoney(prevMonth, currency, { locale })) })}</span>
        )}
        {leftToSpend != null && (
          <span>{t('budgetsPace.leftToSpend', { amount: mask(formatMoney(leftToSpend, currency, { locale })) })}</span>
        )}
        {leftPerDay != null && (
          <span>{t('budgetsPace.leftPerDay', { amount: mask(formatMoney(leftPerDay, currency, { locale })) })}</span>
        )}
      </div>
    </div>
  )
}
