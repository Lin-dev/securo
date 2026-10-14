import { useTranslation } from 'react-i18next'

import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import type { WealthPaceStatus } from '@/types/wealth'

const STATUS_CLASSES: Record<WealthPaceStatus, string> = {
  under: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200',
  on_pace: 'bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-200',
  over: 'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-200',
  no_budget: 'bg-muted text-muted-foreground',
}

/**
 * Small status chip for a budget's pace (plan S7): under / on pace / over /
 * no budget. Colour is never the only signal -- the label itself says the
 * status, so this reads fine without colour vision.
 */
export function PaceBadge({ status, className }: { status: WealthPaceStatus; className?: string }) {
  const { t } = useTranslation()
  return (
    <Badge variant="outline" className={cn(STATUS_CLASSES[status], className)}>
      {t(`budgetsPace.status.${status}`)}
    </Badge>
  )
}
