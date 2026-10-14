import { useTranslation } from 'react-i18next'

import { Button } from '@/components/ui/button'
import type { WealthIncomeCategoryRow, WealthSettingsUpdate } from '@/types/wealth'

import { SettingsCard } from './settings-card'

interface IncomeCategoriesCardProps {
  rows: WealthIncomeCategoryRow[]
  onSave: (body: WealthSettingsUpdate) => void
  disabled?: boolean
}

/** Which categories count as income for the P1 figures. Ships with a default
 *  list (`is_income_like`); picking anything sends the full selected set. */
export function IncomeCategoriesCard({ rows, onSave, disabled }: IncomeCategoriesCardProps) {
  const { t } = useTranslation()

  function toggle(id: string, checked: boolean) {
    const selectedIds = rows.filter((row) => (row.id === id ? checked : row.selected)).map((row) => row.id)
    onSave({ income_category_ids: selectedIds })
  }

  return (
    <SettingsCard
      title={t('wealthSettings.income.title')}
      description={t('wealthSettings.income.help')}
      action={
        <Button
          variant="outline"
          size="sm"
          disabled={disabled}
          onClick={() => onSave({ reset_income_categories: true })}
        >
          {t('wealthSettings.income.useDefaults')}
        </Button>
      }
    >
      {rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t('wealthSettings.income.empty')}</p>
      ) : (
        <ul className="space-y-2">
          {rows.map((row) => (
            <li key={row.id} className="flex items-center gap-2">
              <input
                id={`wealth-income-${row.id}`}
                type="checkbox"
                checked={row.selected}
                disabled={disabled}
                onChange={(e) => toggle(row.id, e.target.checked)}
                className="h-4 w-4 rounded border-border accent-primary"
              />
              <label htmlFor={`wealth-income-${row.id}`} className="text-sm text-foreground">
                {row.name}
                {row.group_name && <span className="text-muted-foreground"> &middot; {row.group_name}</span>}
                {row.is_default && (
                  <span className="text-xs text-muted-foreground">
                    {t('wealthSettings.income.defaultSuffix')}
                  </span>
                )}
              </label>
            </li>
          ))}
        </ul>
      )}
    </SettingsCard>
  )
}
