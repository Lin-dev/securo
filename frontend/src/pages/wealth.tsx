import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'

import { PageHeader } from '@/components/page-header'
import { WEALTH_TABS, wealthTabFromParam, type WealthTab } from '@/lib/wealth-tabs'
import { NetWorthTab } from '@/components/wealth/networth/networth-tab'
import { RoadTab } from '@/components/wealth/road/road-tab'
import { SavingsTab } from '@/components/wealth/savings/savings-tab'
import { SettingsTab } from '@/components/wealth/settings/settings-tab'

/** The road to the user's net-worth milestone (fork). One page, one tab per question;
 *  the tab lives in the URL so a link or a reload lands on the same view. */
export default function WealthPage() {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = wealthTabFromParam(searchParams.get('tab'))

  function selectTab(next: WealthTab) {
    const params = new URLSearchParams(searchParams)
    if (next === WEALTH_TABS[0]) params.delete('tab')
    else params.set('tab', next)
    setSearchParams(params, { replace: true })
  }

  return (
    <div className="space-y-6">
      <PageHeader section={t('wealth.title')} title={t(`wealth.tabs.${tab}`)} />

      <div
        role="tablist"
        aria-label={t('wealth.tabsLabel')}
        className="inline-flex flex-wrap items-center rounded-lg border border-border bg-muted/40 p-0.5"
      >
        {WEALTH_TABS.map((value) => (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={tab === value}
            onClick={() => selectTab(value)}
            className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
              tab === value ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            {t(`wealth.tabs.${value}`)}
          </button>
        ))}
      </div>

      <div role="tabpanel">
        {tab === 'road' && <RoadTab />}
        {tab === 'networth' && <NetWorthTab />}
        {tab === 'savings' && <SavingsTab />}
        {tab === 'settings' && <SettingsTab />}
      </div>
    </div>
  )
}
