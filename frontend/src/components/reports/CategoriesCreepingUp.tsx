import { useTranslation } from 'react-i18next'
import { TrendingUp } from 'lucide-react'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { categoriesCreepingUp, type CategoryCreepCategory } from '@/lib/category-creep'
import { formatCurrency } from '@/lib/format'
import type { CategoryTrendItem } from '@/types'

export interface CreepingUpSelection {
  key: string
  label: string
  /** The latest complete month the spike was found in ("YYYY-MM"). */
  month: string
}

export interface CategoriesCreepingUpProps {
  /** The income/expenses report's own `category_trend`, unfiltered -- this
   * component keeps only the expense group itself (plan R2/S6: "creeping
   * up" is a spending question, mirroring the backend's own expense-only
   * `month_cats`). */
  categoryTrend: readonly CategoryTrendItem[]
  /** The report's bucketing interval; the rule only makes sense monthly. */
  interval: string
  /** The caller's own "today" month ("YYYY-MM"), from the workspace
   * timezone -- never the browser's -- so the still-open current month is
   * never mistaken for a complete one. */
  currentMonth: string
  currency: string
  locale: string
  /** Offered only when the interval isn't already monthly. */
  onSwitchToMonthly?: () => void
  onSelect: (selection: CreepingUpSelection) => void
}

/**
 * "Categories creeping up" (plan R2/S6; critique S13 kept this over small
 * multiples). Built on `lib/category-creep.ts`'s port of the backend's
 * `category_spikes` -- same rule, same thresholds, same edge cases.
 */
export function CategoriesCreepingUp({
  categoryTrend,
  interval,
  currentMonth,
  currency,
  locale,
  onSwitchToMonthly,
  onSelect,
}: CategoriesCreepingUpProps) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()

  const fmt = (value: number) => mask(formatCurrency(value, currency, locale))

  return (
    <div className="bg-card rounded-xl border border-border shadow-sm mb-5">
      <div className="px-5 pt-5 pb-2">
        <p className="text-sm font-semibold text-foreground flex items-center gap-1.5">
          <TrendingUp size={14} className="text-amber-500" aria-hidden="true" />
          {t('reports.creepingUp')}
        </p>
        <p className="text-[11px] text-muted-foreground mt-0.5">{t('reports.creepingUpHint')}</p>
      </div>

      {interval !== 'monthly' ? (
        <div className="px-5 pb-5 pt-1">
          <p className="text-sm text-muted-foreground">{t('reports.creepingUpNeedsMonthly')}</p>
          {onSwitchToMonthly && (
            <button
              type="button"
              onClick={onSwitchToMonthly}
              className="mt-2 text-sm font-semibold text-primary hover:underline"
            >
              {t('reports.creepingUpSwitchToMonthly')}
            </button>
          )}
        </div>
      ) : (
        <CategoriesCreepingUpList
          categoryTrend={categoryTrend}
          currentMonth={currentMonth}
          fmt={fmt}
          onSelect={onSelect}
        />
      )}
    </div>
  )
}

function CategoriesCreepingUpList({
  categoryTrend,
  currentMonth,
  fmt,
  onSelect,
}: {
  categoryTrend: readonly CategoryTrendItem[]
  currentMonth: string
  fmt: (value: number) => string
  onSelect: (selection: CreepingUpSelection) => void
}) {
  const { t } = useTranslation()

  const expenseCategories: CategoryCreepCategory[] = categoryTrend
    .filter((c) => c.group === 'expenses')
    .map((c) => ({
      key: c.key,
      label: c.label,
      color: c.color,
      series: c.series.map((p) => ({ date: p.date, value: p.value })),
    }))

  const items = categoriesCreepingUp(expenseCategories, currentMonth)

  if (items === null) {
    return (
      <p className="px-5 pb-5 pt-1 text-sm text-muted-foreground">
        {t('reports.creepingUpNotEnoughHistory')}
      </p>
    )
  }

  if (items.length === 0) {
    return (
      <p className="px-5 pb-5 pt-1 text-sm text-muted-foreground">
        {t('reports.creepingUpNone')}
      </p>
    )
  }

  return (
    <ul className="px-2 pb-3 divide-y divide-border/60">
      {items.map((item) => (
        <li key={item.key}>
          <button
            type="button"
            onClick={() => onSelect({ key: item.key, label: item.label, month: item.month })}
            aria-label={t('reports.creepingUpRowAria', {
              category: item.label,
              amount: fmt(item.amount),
              excess: fmt(item.excess),
            })}
            className="w-full flex items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-left hover:bg-muted transition-colors"
          >
            <div className="flex items-center gap-2 min-w-0">
              <div className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: item.color }} />
              <span className="text-sm font-medium text-foreground truncate">{item.label}</span>
            </div>
            <div className="text-right shrink-0">
              <p className="text-sm font-bold tabular-nums text-foreground">{fmt(item.amount)}</p>
              <p className="text-[11px] text-muted-foreground tabular-nums">
                {t('reports.creepingUpUsual', { amount: fmt(item.baseline) })}
                {' · '}
                {t('reports.creepingUpExcess', { amount: fmt(item.excess) })}
              </p>
            </div>
          </button>
        </li>
      ))}
    </ul>
  )
}
