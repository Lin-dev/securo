import type { UseQueryResult } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { formatMoney } from '@/lib/chart-format'
import type { WealthLevers, WealthProjection } from '@/types/wealth'

import { describeLever, sortLevers } from './lever-helpers'
import { RoadLeverCard } from './road-lever-card'
import { RoadSectionError, RoadSectionSkeleton } from './road-section-state'

export interface RoadLeversProps {
  projectionQuery: UseQueryResult<WealthProjection>
  /** Enriches `idle_cash`/`cut_top` cards with the current wealth state.
   *  Optional on purpose — if this query fails, the lever list still
   *  renders (idle cash falls back to the projection's own figure; `cut_top`
   *  is skipped without a category to name). */
  leversQuery: UseQueryResult<WealthLevers>
}

/** Lever cards (plan P5), sorted by most months saved first. */
export function RoadLevers({ projectionQuery, leversQuery }: RoadLeversProps) {
  const { t } = useTranslation()

  if (projectionQuery.isError) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base font-semibold">{t('wealthRoad.levers.title')}</CardTitle>
        </CardHeader>
        <CardContent>
          <RoadSectionError onRetry={() => projectionQuery.refetch()} />
        </CardContent>
      </Card>
    )
  }

  if (!projectionQuery.data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base font-semibold">{t('wealthRoad.levers.title')}</CardTitle>
        </CardHeader>
        <CardContent>
          <RoadSectionSkeleton className="h-40 w-full" />
        </CardContent>
      </Card>
    )
  }

  return <RoadLeversBody projection={projectionQuery.data} levers={leversQuery.data} />
}

function RoadLeversBody({ projection, levers }: { projection: WealthProjection; levers: WealthLevers | undefined }) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()
  const money = (value: number) => mask(formatMoney(value, projection.currency, { locale }))

  // Sorting and describing a handful of levers is cheap — no useMemo needed.
  const cards = sortLevers(projection.levers)
    .map((lever) => describeLever(lever, levers, t, money))
    .filter((copy): copy is NonNullable<typeof copy> => copy != null)

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base font-semibold">{t('wealthRoad.levers.title')}</CardTitle>
      </CardHeader>
      <CardContent>
        {cards.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t('wealthRoad.levers.empty')}</p>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {cards.map((copy) => (
              <RoadLeverCard key={copy.key} copy={copy} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
