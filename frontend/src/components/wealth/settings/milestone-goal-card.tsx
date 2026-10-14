import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { goals as goalsApi } from '@/lib/api'
import { formatCurrency } from '@/lib/format'
import type { WealthSettingsUpdate } from '@/types/wealth'

import { SettingsCard } from './settings-card'

/** Not a real goal id — the "no milestone" choice, which sends `clear_goal`. */
const NONE_VALUE = '__none__'

interface MilestoneGoalCardProps {
  goalId: string | null
  onSave: (body: WealthSettingsUpdate) => void
  disabled?: boolean
}

/** The $1M milestone is an existing net-worth Goal (plan P6), picked here
 *  rather than modeled separately. */
export function MilestoneGoalCard({ goalId, onSave, disabled }: MilestoneGoalCardProps) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const goalsQuery = useQuery({ queryKey: ['goals'], queryFn: () => goalsApi.list() })
  const netWorthGoals = (goalsQuery.data ?? []).filter((goal) => goal.tracking_type === 'net_worth')

  function handleChange(value: string) {
    if (value === NONE_VALUE) onSave({ clear_goal: true })
    else onSave({ goal_id: value })
  }

  return (
    <SettingsCard title={t('wealthSettings.milestone.title')}>
      {netWorthGoals.length === 0 ? (
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">{t('wealthSettings.milestone.empty')}</p>
          <Link to="/goals" className="text-sm font-medium text-primary hover:underline">
            {t('wealthSettings.milestone.goToGoals')}
          </Link>
        </div>
      ) : (
        <div className="space-y-1.5">
          <Label htmlFor="wealth-settings-goal">{t('wealthSettings.milestone.label')}</Label>
          <Select value={goalId ?? NONE_VALUE} onValueChange={handleChange} disabled={disabled}>
            <SelectTrigger id="wealth-settings-goal" className="w-full sm:w-96">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={NONE_VALUE}>{t('wealthSettings.milestone.none')}</SelectItem>
              {netWorthGoals.map((goal) => (
                <SelectItem key={goal.id} value={goal.id}>
                  {t('wealthSettings.milestone.optionLabel', {
                    name: goal.name,
                    amount: formatCurrency(goal.target_amount, goal.currency, locale),
                  })}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}
    </SettingsCard>
  )
}
