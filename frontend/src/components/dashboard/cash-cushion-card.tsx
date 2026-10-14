import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { Skeleton } from '@/components/ui/skeleton'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { formatMoney } from '@/lib/chart-format'

export interface CashCushionCardProps {
  /** Months of typical spending the cash balance covers; null when there
   *  isn't enough spending history to compute it yet. */
  cashCushionMonths: number | null
  /** Cash above the buffer (plan P3); shown only when it is above 0. */
  idleCash: number
  currency: string
  isLoading: boolean
}

/**
 * Dashboard card (plan S3): how many months of spending the cash on hand
 * covers, and how much of it sits idle above the buffer. Shown only when
 * the workspace has the `wealth` module.
 *
 * Pure HTML/CSS, matching the dashboard's existing smaller-card chrome —
 * no recharts import here, so it stays inside the entry bundle's +2KB
 * budget (plan P8).
 */
export function CashCushionCard({ cashCushionMonths, idleCash, currency, isLoading }: CashCushionCardProps) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const locale = useDisplayLocale()

  const monthsText =
    cashCushionMonths != null
      ? new Intl.NumberFormat(locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(cashCushionMonths)
      : null
  const showIdleCash = idleCash > 0

  return (
    <div className="bg-card rounded-xl border border-border shadow-sm mb-5">
      <div className="px-5 py-4 border-b border-border flex items-center justify-between">
        <p className="text-sm font-semibold text-foreground">{t('dashboard.wealthCashCushionTitle')}</p>
        <Link to="/wealth" className="text-xs font-medium text-primary hover:underline">
          {t('dashboard.wealthCashCushionViewLink')} &rarr;
        </Link>
      </div>
      <div className="px-5 py-4 space-y-2">
        {isLoading ? (
          <Skeleton className="h-10 w-48" />
        ) : (
          <>
            <p className="text-lg font-bold tabular-nums text-foreground">
              {monthsText != null
                ? t('dashboard.wealthCashCushionMonths', { months: mask(monthsText) })
                : t('dashboard.wealthCashCushionUnknown')}
            </p>
            {showIdleCash && (
              <p className="text-sm text-muted-foreground">
                {t('dashboard.wealthIdleCashAboveBuffer', {
                  amount: mask(formatMoney(idleCash, currency, { locale })),
                })}
              </p>
            )}
          </>
        )}
        <Link to="/wealth?tab=settings" className="inline-block text-xs font-medium text-primary hover:underline">
          {t('dashboard.wealthCashCushionChangeBuffer')} &rarr;
        </Link>
      </div>
    </div>
  )
}
