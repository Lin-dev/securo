import { useTranslation } from 'react-i18next'
import type { UseQueryResult } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { useDateLocale, useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { formatMoney } from '@/lib/chart-format'
import type { WealthRecurringSuggestion, WealthSubscriptionAudit } from '@/types/wealth'

import { formatCashDay } from './cash-format'
import { RecurringSuggestionsCard } from './recurring-suggestions-card'

export interface SubscriptionsSectionProps {
  query: UseQueryResult<WealthSubscriptionAudit>
  onCreate: (suggestion: WealthRecurringSuggestion) => void
  isCreating: (merchant: string) => boolean
}

/**
 * The subscription audit (plan B5b/S8): every recurring charge's monthly
 * and annual cost, with the running monthly/annual totals, plus "Not
 * tracked yet" suggestions the detector found with no recurring rule.
 */
export function SubscriptionsSection({ query, onCreate, isCreating }: SubscriptionsSectionProps) {
  const { t } = useTranslation()

  if (query.isError) {
    return (
      <div className="bg-card rounded-xl border border-border shadow-sm p-5">
        <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-destructive">
          <span>{t('common.error')}</span>
          <Button variant="outline" size="sm" onClick={() => query.refetch()}>
            {t('common.retry')}
          </Button>
        </div>
      </div>
    )
  }

  if (!query.data) {
    return (
      <div className="bg-card rounded-xl border border-border shadow-sm p-5">
        <Skeleton className="h-24 w-full" role="status" aria-label={t('common.loading')} />
      </div>
    )
  }

  return <SubscriptionsSectionBody audit={query.data} onCreate={onCreate} isCreating={isCreating} />
}

function SubscriptionsSectionBody({
  audit,
  onCreate,
  isCreating,
}: {
  audit: WealthSubscriptionAudit
  onCreate: (suggestion: WealthRecurringSuggestion) => void
  isCreating: (merchant: string) => boolean
}) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const dateLocale = useDateLocale()
  const { mask } = usePrivacyMode()
  const money = (value: number) => mask(formatMoney(value, audit.currency, { locale }))

  return (
    <div className="space-y-4">
      <div className="bg-card rounded-xl border border-border shadow-sm overflow-hidden">
        <div className="px-4 sm:px-5 py-4 border-b border-border flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm font-semibold text-foreground">{t('wealthCash.subscriptions.title')}</p>
          <div className="flex flex-wrap items-center gap-4 text-sm text-muted-foreground">
            <span>{t('wealthCash.subscriptions.monthlyTotal', { amount: money(audit.monthly_total) })}</span>
            <span>{t('wealthCash.subscriptions.annualTotal', { amount: money(audit.annual_total) })}</span>
          </div>
        </div>

        {audit.subscriptions.length === 0 ? (
          <p className="text-sm text-muted-foreground text-center py-10">{t('wealthCash.subscriptions.empty')}</p>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-border">
                <th className="text-xs font-medium text-muted-foreground py-3 pl-4 sm:pl-5 text-left">
                  {t('wealthCash.subscriptions.merchant')}
                </th>
                <th className="text-xs font-medium text-muted-foreground py-3 text-left">
                  {t('wealthCash.subscriptions.monthly')}
                </th>
                <th className="text-xs font-medium text-muted-foreground py-3 text-left hidden sm:table-cell">
                  {t('wealthCash.subscriptions.annual')}
                </th>
                <th className="text-xs font-medium text-muted-foreground py-3 pr-4 sm:pr-5 text-left hidden md:table-cell">
                  {t('wealthCash.subscriptions.lastCharge')}
                </th>
              </tr>
            </thead>
            <tbody>
              {audit.subscriptions.map((sub) => (
                <tr key={sub.merchant} className="border-b border-border last:border-0">
                  <td className="py-3 pl-4 sm:pl-5 text-sm font-medium text-foreground">
                    {sub.merchant}
                    {sub.recurring_id && (
                      <Link to="/recurring" className="ml-2 text-xs font-medium text-primary hover:underline">
                        {t('wealthCash.subscriptions.viewRule')}
                      </Link>
                    )}
                  </td>
                  <td className="py-3 text-sm tabular-nums text-foreground">{money(sub.monthly)}</td>
                  <td className="py-3 text-sm tabular-nums text-muted-foreground hidden sm:table-cell">
                    {money(sub.annual)}
                  </td>
                  <td className="py-3 pr-4 sm:pr-5 text-xs text-muted-foreground hidden md:table-cell">
                    {sub.last_charge ? formatCashDay(sub.last_charge, dateLocale) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <RecurringSuggestionsCard
        suggestions={audit.suggestions}
        currency={audit.currency}
        locale={locale}
        onCreate={onCreate}
        isCreating={isCreating}
      />
    </div>
  )
}
