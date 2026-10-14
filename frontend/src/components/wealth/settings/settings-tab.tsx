import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { extractApiError } from '@/lib/api-errors'
import { wealth } from '@/lib/api'
import type { WealthSettingsRead, WealthSettingsUpdate } from '@/types/wealth'

import { AccountsCard } from './accounts-card'
import { AssumptionsCard } from './assumptions-card'
import { IncomeCategoriesCard } from './income-categories-card'
import { MilestoneGoalCard } from './milestone-goal-card'
import { OneOffsCard } from './one-offs-card'

/**
 * The settings tab of /wealth (plan P6, S4): the milestone goal, the P5/P3
 * assumptions, account bucket overrides, income categories and the one-off
 * transactions every other wealth figure is computed from. `PUT` is a
 * partial merge, so each card sends only the keys it changed; the response
 * is the full settings read, which replaces the cache entry here.
 */
export function SettingsTab() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()

  const settingsQuery = useQuery({
    queryKey: ['wealth', 'settings'],
    queryFn: wealth.settings,
  })

  const updateSettings = useMutation({
    mutationFn: (body: WealthSettingsUpdate) => wealth.updateSettings(body),
    onSuccess: (data: WealthSettingsRead) => {
      queryClient.setQueryData(['wealth', 'settings'], data)
      // Every wealth figure depends on settings (plan P6), so the whole
      // namespace — not just this query — needs to be treated as stale.
      void queryClient.invalidateQueries({ queryKey: ['wealth'] })
      toast.success(t('wealthSettings.saved'))
    },
    onError: (err: unknown) => toast.error(extractApiError(err, t('common.error'))),
  })

  function save(body: WealthSettingsUpdate) {
    updateSettings.mutate(body)
  }

  if (settingsQuery.isError) {
    return (
      <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-destructive">
        <span>{t('common.error')}</span>
        <Button variant="outline" size="sm" onClick={() => settingsQuery.refetch()}>
          {t('common.retry')}
        </Button>
      </div>
    )
  }

  if (!settingsQuery.data) {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        {t('common.loading')}
      </p>
    )
  }

  const { values, sources, accounts, income_categories, one_offs } = settingsQuery.data
  const disabled = updateSettings.isPending

  return (
    <div className="space-y-6">
      <MilestoneGoalCard goalId={values.goal_id} onSave={save} disabled={disabled} />
      <AssumptionsCard
        returnNominal={values.projection.return_nominal}
        withdrawalRate={values.projection.withdrawal_rate}
        bufferMonths={values.buffer_months}
        projectionSource={sources.projection ?? 'default'}
        bufferSource={sources.buffer_months ?? 'default'}
        onSave={save}
        disabled={disabled}
      />
      <AccountsCard rows={accounts} onSave={save} disabled={disabled} />
      <IncomeCategoriesCard rows={income_categories} onSave={save} disabled={disabled} />
      <OneOffsCard rows={one_offs} onSave={save} disabled={disabled} />
    </div>
  )
}
