import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ChevronDown, ChevronUp } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { formatMoney } from '@/lib/chart-format'
import type { WealthBudgetSuggestion, WealthBudgetSuggestions } from '@/types/wealth'

export interface SuggestionsPanelProps {
  data: WealthBudgetSuggestions
  currency: string
  locale: string
  expanded: boolean
  onToggleExpanded: () => void
  onCreate: (categoryIds: string[]) => void
  isPending: boolean
}

/**
 * Budget suggestions from history (plan B5a/S7): the median of each
 * category's last few full months, shown as a checklist so the person can
 * pick which ones to create. The entry point to this page while production
 * has 0 budgets -- collapsible once budgets exist (owner decides when).
 */
export function SuggestionsPanel({
  data,
  currency,
  locale,
  expanded,
  onToggleExpanded,
  onCreate,
  isPending,
}: SuggestionsPanelProps) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  // Only the person's own toggles live in state. A category that already
  // has a budget defaults unchecked (the suggestion is informational
  // there); anything else defaults checked -- computed fresh each render,
  // so there's nothing to resync when the suggestions themselves change.
  const [overrides, setOverrides] = useState<Record<string, boolean>>({})

  function isChecked(suggestion: WealthBudgetSuggestion): boolean {
    return overrides[suggestion.category_id] ?? suggestion.existing_budget == null
  }

  const checkedIds = data.suggestions.filter(isChecked).map((s) => s.category_id)
  const typical = data.typical_monthly_spending != null ? Number(data.typical_monthly_spending) : null

  return (
    <div className="bg-card rounded-xl border border-border shadow-sm overflow-hidden mb-5">
      <div className="px-4 sm:px-5 py-4 border-b border-border flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-semibold text-foreground">
          {t('budgetsPace.suggestions.title', { count: data.months.length })}
        </p>
        <button
          type="button"
          aria-expanded={expanded}
          onClick={onToggleExpanded}
          className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors cursor-pointer"
        >
          {expanded ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
          {expanded ? t('budgetsPace.suggestions.collapse') : t('budgetsPace.suggestions.expand')}
        </button>
      </div>

      {expanded && (
        <div className="p-4 sm:p-5 space-y-4">
          <ul className="divide-y divide-border/40">
            {data.suggestions.map((suggestion) => (
              <li
                key={suggestion.category_id}
                className="flex flex-wrap items-center justify-between gap-2 py-2.5 first:pt-0 last:pb-0"
              >
                <label className="flex items-center gap-2.5 min-w-0 cursor-pointer">
                  <input
                    type="checkbox"
                    className="rounded border-border"
                    checked={isChecked(suggestion)}
                    onChange={(e) =>
                      setOverrides((prev) => ({ ...prev, [suggestion.category_id]: e.target.checked }))
                    }
                    aria-label={t('budgetsPace.suggestions.checkboxLabel', { category: suggestion.category })}
                  />
                  <span className="truncate text-sm font-medium text-foreground">{suggestion.category}</span>
                  {suggestion.existing_budget != null && (
                    <span className="text-xs text-muted-foreground shrink-0">
                      {t('budgetsPace.suggestions.existingBudget', {
                        amount: mask(formatMoney(Number(suggestion.existing_budget), currency, { locale })),
                      })}
                    </span>
                  )}
                </label>
                <span className="text-sm tabular-nums text-muted-foreground shrink-0">
                  {t('budgetsPace.suggestions.median', {
                    amount: mask(formatMoney(Number(suggestion.monthly_median), currency, { locale })),
                  })}
                </span>
              </li>
            ))}
          </ul>

          <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
            <div className="min-w-0">
              <p className="text-sm text-foreground">
                {t('budgetsPace.suggestions.totals', {
                  total: mask(formatMoney(Number(data.total_suggested), currency, { locale })),
                  typical: mask(formatMoney(typical, currency, { locale })),
                })}
              </p>
              <p className="text-xs text-muted-foreground mt-0.5">{t('budgetsPace.suggestions.totalsNote')}</p>
            </div>
            <Button size="sm" disabled={checkedIds.length === 0 || isPending} onClick={() => onCreate(checkedIds)}>
              {t('budgetsPace.suggestions.createButton', { count: checkedIds.length })}
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
