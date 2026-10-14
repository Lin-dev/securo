import { useTranslation } from 'react-i18next'
import type { UseQueryResult } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { useDateLocale, useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { formatMoney } from '@/lib/chart-format'
import type { WealthUpcoming, WealthUpcomingKind } from '@/types/wealth'

import { formatCashDay } from './cash-format'
import { groupUpcomingByDate, isOnCardAccount } from './upcoming-timeline-data'

export interface UpcomingTimelineProps {
  query: UseQueryResult<WealthUpcoming>
}

const KIND_LABEL_KEYS: Record<WealthUpcomingKind, string> = {
  recurring: 'wealthCash.timeline.kind.recurring',
  card_bill: 'wealthCash.timeline.kind.cardBill',
  installment: 'wealthCash.timeline.kind.installment',
  pending: 'wealthCash.timeline.kind.pending',
}

/**
 * "Next 30 days" (plan B5b/S8): upcoming charges grouped by date, each with
 * its account, a kind badge, and the amount. A card purchase (a `recurring`
 * charge on a card account) is marked "on card" -- the server's `total`
 * already leaves it out, since it only reaches cash through the card's bill.
 */
export function UpcomingTimeline({ query }: UpcomingTimelineProps) {
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

  return <UpcomingTimelineBody upcoming={query.data} />
}

function UpcomingTimelineBody({ upcoming }: { upcoming: WealthUpcoming }) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const dateLocale = useDateLocale()
  const { mask } = usePrivacyMode()

  const groups = groupUpcomingByDate(upcoming.items)

  return (
    <div className="bg-card rounded-xl border border-border shadow-sm overflow-hidden">
      <div className="px-4 sm:px-5 py-4 border-b border-border flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-semibold text-foreground">{t('wealthCash.timeline.title')}</p>
        <p className="text-sm font-medium text-foreground tabular-nums">
          {t('wealthCash.timeline.total', { amount: mask(formatMoney(upcoming.total, upcoming.currency, { locale })) })}
        </p>
      </div>

      {groups.length === 0 ? (
        <p className="text-sm text-muted-foreground text-center py-10">{t('wealthCash.timeline.empty')}</p>
      ) : (
        <div className="divide-y divide-border">
          {groups.map((group) => (
            <div key={group.date} className="px-4 sm:px-5 py-3">
              <p className="text-xs font-semibold text-muted-foreground mb-2">{formatCashDay(group.date, dateLocale)}</p>
              <ul className="space-y-2">
                {group.items.map((item, index) => {
                  const onCard = isOnCardAccount(item)
                  return (
                    <li
                      key={`${group.date}-${index}-${item.description}`}
                      className="flex flex-wrap items-center justify-between gap-2"
                    >
                      <div className="min-w-0 flex items-center gap-2 flex-wrap">
                        <span className="text-sm font-medium text-foreground truncate">{item.description}</span>
                        <Badge variant="outline">{t(KIND_LABEL_KEYS[item.kind])}</Badge>
                        {onCard && <Badge variant="secondary">{t('wealthCash.timeline.onCard')}</Badge>}
                        {item.account && <span className="text-xs text-muted-foreground">{item.account}</span>}
                        {item.recurring_id && (
                          <Link to="/recurring" className="text-xs font-medium text-primary hover:underline">
                            {t('wealthCash.timeline.viewRule')}
                          </Link>
                        )}
                      </div>
                      <span className="text-sm font-medium tabular-nums text-foreground shrink-0">
                        {mask(formatMoney(item.amount, upcoming.currency, { locale }))}
                      </span>
                    </li>
                  )
                })}
              </ul>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
