import type { UseQueryResult } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { formatMoney } from '@/lib/chart-format'
import type { WealthLevers } from '@/types/wealth'

import { RoadSectionError, RoadSectionSkeleton } from './road-section-state'

export interface RoadThisMonthProps {
  leversQuery: UseQueryResult<WealthLevers>
}

/**
 * "This month's move": the one, code-written action for this month
 * (`levers.move.text`), the cash cushion and the idle cash, plus a neutral
 * note when the typical month invests more than it saves (plan P5/P1).
 */
export function RoadThisMonth({ leversQuery }: RoadThisMonthProps) {
  const { t } = useTranslation()

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base font-semibold">{t('wealthRoad.thisMonth.title')}</CardTitle>
      </CardHeader>
      <CardContent>
        {leversQuery.isError ? (
          <RoadSectionError onRetry={() => leversQuery.refetch()} />
        ) : !leversQuery.data ? (
          <RoadSectionSkeleton className="h-24 w-full" />
        ) : (
          <RoadThisMonthBody levers={leversQuery.data} />
        )}
      </CardContent>
    </Card>
  )
}

function RoadThisMonthBody({ levers }: { levers: WealthLevers }) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const { privacyMode, mask } = usePrivacyMode()
  const money = (value: number) => mask(formatMoney(value, levers.currency, { locale }))

  // `move.text` is code-written and already carries a formatted amount
  // (plan: "this_month_move() ... deterministic"). Privacy mode can't mask
  // just the number inside server-written prose, so it swaps in a generic
  // sentence instead of leaking the amount.
  const moveText = levers.move
    ? privacyMode
      ? t('wealthRoad.thisMonth.moveMasked')
      : levers.move.text
    : t('wealthRoad.thisMonth.noMove')

  const cushionText =
    levers.cash_cushion_months == null
      ? t('wealthRoad.thisMonth.cushionUnknown')
      : t('wealthRoad.thisMonth.cushion', {
          months: new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(levers.cash_cushion_months),
        })

  return (
    <div className="space-y-3">
      <p className="text-sm font-medium text-foreground">{moveText}</p>
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-muted-foreground">
        <span>{cushionText}</span>
        <span>{t('wealthRoad.thisMonth.idleCash', { amount: money(levers.idle_cash) })}</span>
      </div>
      {levers.investing_exceeds_saving && (
        <p className="text-xs text-muted-foreground">{t('wealthRoad.thisMonth.investingExceedsSaving')}</p>
      )}
    </div>
  )
}
