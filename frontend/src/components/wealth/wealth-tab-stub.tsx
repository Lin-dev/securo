import { useTranslation } from 'react-i18next'

import type { WealthTab } from '@/lib/wealth-tabs'

/** Placeholder each R1 slice replaces with its tab. A tab still on this stub at
 *  release time is taken out of WEALTH_TABS instead of shipping (plan P12). */
export function WealthTabStub({ tab }: { tab: WealthTab }) {
  const { t } = useTranslation()
  return (
    <div className="rounded-xl border border-dashed border-border bg-card p-8 text-center">
      <p className="text-sm font-medium text-foreground">{t(`wealth.tabs.${tab}`)}</p>
      <p className="mt-1 text-sm text-muted-foreground">{t('wealth.stub')}</p>
    </div>
  )
}
