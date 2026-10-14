import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { extractApiError } from '@/lib/api-errors'
import { goals as goalsApi, wealth } from '@/lib/api'
import { useRegisterPageChatContext } from '@/lib/page-chat-context'
import type { WealthSettingsUpdate } from '@/types/wealth'

import { RoadHero } from './road-hero'
import { RoadLevers } from './road-levers'
import { RoadProjectionChart } from './road-projection-chart'
import { RoadThisMonth } from './road-this-month'

/** Months of actual history charted alongside the projection. */
const NET_WORTH_HISTORY_MONTHS = 12

/**
 * The Road tab of /wealth (plan R1 S1): "when do I reach my net-worth goal,
 * and what should I do this month?" Four queries back four independent
 * sections — the hero, the projection chart, the lever cards and this
 * month's move — so one section's query failing never blanks the others.
 */
export function RoadTab() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()

  const summaryQuery = useQuery({ queryKey: ['wealth', 'summary'], queryFn: wealth.summary })
  const projectionQuery = useQuery({ queryKey: ['wealth', 'projection'], queryFn: wealth.projection })
  const leversQuery = useQuery({ queryKey: ['wealth', 'levers'], queryFn: wealth.levers })
  const historyQuery = useQuery({
    queryKey: ['wealth', 'net-worth-history', NET_WORTH_HISTORY_MONTHS],
    queryFn: () => wealth.netWorthHistory(NET_WORTH_HISTORY_MONTHS),
  })

  // User-initiated only (plan P6: never created automatically). Creates the
  // $1M net-worth Goal, then points wealth settings at it, then refreshes
  // every wealth view and the Goals page.
  const createGoal = useMutation({
    mutationFn: async () => {
      const currency = summaryQuery.data?.currency ?? 'USD'
      const createdGoal = await goalsApi.create({
        name: t('wealthRoad.hero.createGoalName'),
        target_amount: 1_000_000,
        currency,
        tracking_type: 'net_worth',
      })
      const body: WealthSettingsUpdate = { goal_id: createdGoal.id }
      await wealth.updateSettings(body)
      return createdGoal
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['wealth'] })
      void queryClient.invalidateQueries({ queryKey: ['goals'] })
    },
    onError: (err: unknown) => toast.error(extractApiError(err, t('common.error'))),
  })

  const levers = projectionQuery.data?.levers ?? []
  const summary = summaryQuery.data
  const contextKey = JSON.stringify({
    goal: summary?.goal?.target ?? null,
    label: summary?.projected_label ?? null,
    days: summary?.coverage.days ?? null,
    levers: levers.map((lever) => [lever.key, lever.shift_months]),
  })

  // Free-form page state for the chat (lib/page-chat-context.tsx) — never
  // raw transaction data, just the numbers this tab itself is built on.
  useRegisterPageChatContext(
    {
      path: '/wealth',
      tab: 'road',
      goalTarget: summary?.goal?.target ?? null,
      projectedLabel: summary?.projected_label ?? null,
      coverageDays: summary?.coverage.days ?? null,
      levers: levers.map((lever) => ({ key: lever.key, shiftMonths: lever.shift_months })),
    },
    contextKey,
  )

  return (
    <div className="space-y-6">
      <RoadHero summaryQuery={summaryQuery} onCreateGoal={() => createGoal.mutate()} creatingGoal={createGoal.isPending} />
      <RoadProjectionChart projectionQuery={projectionQuery} historyQuery={historyQuery} />
      <RoadLevers projectionQuery={projectionQuery} leversQuery={leversQuery} />
      <RoadThisMonth leversQuery={leversQuery} />
    </div>
  )
}
