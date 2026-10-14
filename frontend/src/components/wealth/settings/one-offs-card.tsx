import { useTranslation } from 'react-i18next'

import { Badge } from '@/components/ui/badge'
import { Switch } from '@/components/ui/switch'
import { useAuth } from '@/contexts/auth-context'
import { useDateLocale, useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { formatCurrency } from '@/lib/format'
import type { WealthOneOffRow, WealthSettingsUpdate } from '@/types/wealth'

import { SettingsCard } from './settings-card'

interface OneOffsCardProps {
  rows: WealthOneOffRow[]
  onSave: (body: WealthSettingsUpdate) => void
  disabled?: boolean
}

/** One-off transactions (plan P1): rows the detector proposed, plus whatever
 *  the person switched on or off themselves. Excluded from "typical" figures,
 *  never from "all". Amounts mask in privacy mode like everywhere else. */
export function OneOffsCard({ rows, onSave, disabled }: OneOffsCardProps) {
  const { t } = useTranslation()
  const { user } = useAuth()
  const locale = useDisplayLocale()
  const dateLocale = useDateLocale()
  const { mask } = usePrivacyMode()
  const currency = user?.preferences?.currency_display ?? 'USD'

  return (
    <SettingsCard title={t('wealthSettings.oneOffs.title')} description={t('wealthSettings.oneOffs.help')}>
      {rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t('wealthSettings.oneOffs.empty')}</p>
      ) : (
        <ul className="divide-y divide-border/40">
          {rows.map((row) => (
            <li
              key={row.transaction_id}
              className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-medium text-foreground">{row.description}</span>
                  {row.proposed && <Badge variant="outline">{t('wealthSettings.oneOffs.proposed')}</Badge>}
                </div>
                <div className="text-xs text-muted-foreground">
                  {new Date(`${row.date}T00:00:00`).toLocaleDateString(dateLocale)}
                </div>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-sm font-medium tabular-nums text-foreground">
                  {mask(formatCurrency(row.amount, currency, locale))}
                </span>
                <span id={`wealth-oneoff-label-${row.transaction_id}`} className="sr-only">
                  {t('wealthSettings.oneOffs.toggleLabel', { description: row.description })}
                </span>
                <Switch
                  checked={row.selected}
                  disabled={disabled}
                  aria-labelledby={`wealth-oneoff-label-${row.transaction_id}`}
                  onCheckedChange={(checked) =>
                    onSave({ one_off_overrides: { [row.transaction_id]: checked } })
                  }
                />
              </div>
            </li>
          ))}
        </ul>
      )}
    </SettingsCard>
  )
}
