import { useTranslation } from 'react-i18next'

import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'

/**
 * Shared loading/error presentation for a Road tab section. Each section
 * (hero, chart, levers, this month's move) reads its own query, so one
 * query failing shows this in its own card instead of blanking the page.
 */
export function RoadSectionSkeleton({ className = 'h-24 w-full' }: { className?: string }) {
  const { t } = useTranslation()
  return <Skeleton role="status" aria-label={t('common.loading')} className={className} />
}

export function RoadSectionError({ onRetry }: { onRetry: () => void }) {
  const { t } = useTranslation()
  return (
    <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-destructive">
      <span>{t('common.error')}</span>
      <Button variant="outline" size="sm" onClick={onRetry}>
        {t('common.retry')}
      </Button>
    </div>
  )
}
