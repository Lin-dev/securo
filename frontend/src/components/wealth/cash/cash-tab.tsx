import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import { extractApiError } from '@/lib/api-errors'
import { recurring as recurringApi, wealth } from '@/lib/api'
import { invalidateFinancialQueries } from '@/lib/invalidate-queries'
import type { RecurringTransaction } from '@/types'
import type { WealthRecurringSuggestion } from '@/types/wealth'

import { CashForecastChart } from './cash-forecast-chart'
import { SubscriptionsSection } from './subscriptions-section'
import { UpcomingTimeline } from './upcoming-timeline'

const UPCOMING_DAYS = 30
const FORECAST_DAYS = 90

/**
 * The cash tab of /wealth (plan R2 B5b/S8): the next 30 days of charges, the
 * 90-day cash forecast, and the subscription audit. Three independent
 * queries, same shape as the Road tab -- one section's query failing never
 * blanks the others.
 */
export function CashTab() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [creatingMerchant, setCreatingMerchant] = useState<string | null>(null)

  const forecastQuery = useQuery({
    queryKey: ['wealth', 'cash-forecast', FORECAST_DAYS],
    queryFn: () => wealth.cashForecast(FORECAST_DAYS),
  })
  const upcomingQuery = useQuery({
    queryKey: ['wealth', 'upcoming', UPCOMING_DAYS],
    queryFn: () => wealth.upcoming(UPCOMING_DAYS),
  })
  const subscriptionsQuery = useQuery({
    queryKey: ['wealth', 'subscriptions'],
    queryFn: wealth.subscriptions,
  })

  // "Add as recurring" posts the suggestion's `create_payload` as-is to the
  // existing recurring-create call (plan B5b), then refreshes both the
  // Recurring list and every wealth view that could now show it tracked.
  const addSuggestionMutation = useMutation({
    mutationFn: (suggestion: WealthRecurringSuggestion) =>
      recurringApi.create(suggestion.create_payload as Partial<RecurringTransaction>),
    onMutate: (suggestion: WealthRecurringSuggestion) => setCreatingMerchant(suggestion.merchant),
    onSuccess: () => {
      invalidateFinancialQueries(queryClient)
      queryClient.invalidateQueries({ queryKey: ['recurring'] })
      toast.success(t('recurring.created'))
    },
    onError: (err: unknown) => toast.error(extractApiError(err, t('common.error'))),
    onSettled: () => setCreatingMerchant(null),
  })

  return (
    <div className="space-y-6">
      <CashForecastChart query={forecastQuery} />
      <UpcomingTimeline query={upcomingQuery} />
      <SubscriptionsSection
        query={subscriptionsQuery}
        onCreate={(suggestion) => addSuggestionMutation.mutate(suggestion)}
        isCreating={(merchant) => creatingMerchant === merchant}
      />
    </div>
  )
}
