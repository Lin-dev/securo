import type { UseQueryResult } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

import { BulletBar } from '@/components/charts/BulletBar'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { formatMoney, formatPct } from '@/lib/chart-format'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import type { WealthSummary } from '@/types/wealth'

import { RoadSectionError, RoadSectionSkeleton } from './road-section-state'

export interface RoadHeroProps {
  summaryQuery: UseQueryResult<WealthSummary>
  onCreateGoal: () => void
  creatingGoal: boolean
}

/**
 * The hero: the goal's progress (when one exists), the projected $1M date,
 * the data-coverage note, and — when there's no net-worth Goal yet — the
 * user-initiated "Create my $1M goal" button (plan P6; never created
 * automatically).
 */
export function RoadHero({ summaryQuery, onCreateGoal, creatingGoal }: RoadHeroProps) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base font-semibold">{t('wealthRoad.hero.title')}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {summaryQuery.isError ? (
          <RoadSectionError onRetry={() => summaryQuery.refetch()} />
        ) : !summaryQuery.data ? (
          <RoadSectionSkeleton className="h-32 w-full" />
        ) : (
          <HeroBody summary={summaryQuery.data} onCreateGoal={onCreateGoal} creatingGoal={creatingGoal} locale={locale} mask={mask} />
        )}
      </CardContent>
    </Card>
  )
}

function HeroBody({
  summary,
  onCreateGoal,
  creatingGoal,
  locale,
  mask,
}: {
  summary: WealthSummary
  onCreateGoal: () => void
  creatingGoal: boolean
  locale: string
  mask: (value: string) => string
}) {
  const { t } = useTranslation()
  const { goal, coverage, currency, projected_label } = summary
  const projectedText = projected_label ?? t('wealthRoad.hero.notWithin30Years')

  return (
    <div className="space-y-4">
      {goal ? (
        <div className="space-y-2">
          <div className="flex items-baseline justify-between gap-3">
            <span className="text-2xl font-semibold tabular-nums">
              {mask(formatMoney(goal.current, currency, { locale }))}
            </span>
            <span className="text-sm text-muted-foreground">
              {t('wealthRoad.hero.ofTarget', { target: mask(formatMoney(goal.target, currency, { locale })) })}
            </span>
          </div>
          <BulletBar value={goal.current} target={goal.target} currency={currency} />
          <p className="text-sm font-medium text-foreground">
            {t('wealthRoad.hero.percentToGoal', { percent: formatPct(goal.pct, 0, locale) })}
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">{t('wealthRoad.hero.noGoal')}</p>
          <Button onClick={onCreateGoal} disabled={creatingGoal}>
            {t('wealthRoad.hero.createGoal')}
          </Button>
        </div>
      )}

      <div className="space-y-1 border-t border-border/60 pt-3">
        <p className="text-sm text-foreground">{t('wealthRoad.hero.projected', { label: projectedText })}</p>
        <p className="text-xs text-muted-foreground">{t('wealthRoad.hero.coverage', { days: coverage.days })}</p>
        {coverage.insufficient_history && (
          <p className="text-xs text-muted-foreground">{t('wealthRoad.hero.notAnnualized')}</p>
        )}
        <p className="text-xs text-muted-foreground">{t('wealthRoad.hero.nominalNote')}</p>
      </div>
    </div>
  )
}
