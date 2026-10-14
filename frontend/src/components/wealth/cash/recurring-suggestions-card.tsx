import { useTranslation } from 'react-i18next'

import { Button } from '@/components/ui/button'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { formatMoney } from '@/lib/chart-format'
import { cn } from '@/lib/utils'
import type { WealthRecurringSuggestion } from '@/types/wealth'

export interface RecurringSuggestionsCardProps {
  suggestions: WealthRecurringSuggestion[]
  currency: string
  locale: string
  onCreate: (suggestion: WealthRecurringSuggestion) => void
  isCreating: (merchant: string) => boolean
  className?: string
}

/**
 * "Not tracked yet" (plan B5b/S8): recurring charges the detector found with
 * no recurring rule yet, each with an "Add as recurring" button that posts
 * its `create_payload` as-is. Shared between the Cash tab (next to the full
 * subscription audit) and the Recurring page (on its own, compact) -- it
 * imports nothing from the chart kit, since the Recurring page's entry
 * bundle has no budget for it (plan P8).
 */
export function RecurringSuggestionsCard({
  suggestions,
  currency,
  locale,
  onCreate,
  isCreating,
  className,
}: RecurringSuggestionsCardProps) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()

  if (suggestions.length === 0) return null

  return (
    <div className={cn('bg-card rounded-xl border border-border shadow-sm overflow-hidden', className)}>
      <div className="px-4 sm:px-5 py-4 border-b border-border">
        <p className="text-sm font-semibold text-foreground">{t('wealthCash.suggestions.title')}</p>
        <p className="text-xs text-muted-foreground mt-0.5">{t('wealthCash.suggestions.subtitle')}</p>
      </div>
      <ul className="divide-y divide-border/40 px-4 sm:px-5">
        {suggestions.map((suggestion) => (
          <li key={suggestion.merchant} className="flex flex-wrap items-center justify-between gap-2 py-3">
            <div className="min-w-0">
              <p className="text-sm font-medium text-foreground truncate">{suggestion.merchant}</p>
              <p className="text-xs text-muted-foreground">
                {t('wealthCash.suggestions.detail', {
                  amount: mask(formatMoney(suggestion.monthly, currency, { locale })),
                  count: suggestion.months,
                })}
              </p>
            </div>
            <Button
              size="sm"
              variant="outline"
              disabled={isCreating(suggestion.merchant)}
              onClick={() => onCreate(suggestion)}
            >
              {t('wealthCash.suggestions.addAction')}
            </Button>
          </li>
        ))}
      </ul>
    </div>
  )
}
