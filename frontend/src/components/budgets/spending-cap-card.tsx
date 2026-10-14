import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { BulletBar } from '@/components/charts/BulletBar'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { formatMoney } from '@/lib/chart-format'
import { formatAmountInput, parseAmountInput } from '@/lib/format'
import type { WealthBudgetPace } from '@/types/wealth'

import { PaceBadge } from './pace-badge'

export interface SpendingCapCardProps {
  pace: WealthBudgetPace | undefined
  isLoading: boolean
  currency: string
  locale: string
  /** Prefill for the editor when no cap is set yet. */
  typicalMonthlySpending: number | null
  onSetCap: (amount: number) => void
  onClearCap: () => void
  isSaving: boolean
}

/**
 * The monthly spending cap (plan B5a/S7): a bullet bar of total spend vs.
 * the cap, with the same pace marker and badge a budget row gets, plus an
 * inline editor to set, change or clear it. With no cap set, prompts the
 * person to pick one, prefilled with the typical monthly spend when known.
 */
export function SpendingCapCard({
  pace,
  isLoading,
  currency,
  locale,
  typicalMonthlySpending,
  onSetCap,
  onClearCap,
  isSaving,
}: SpendingCapCardProps) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [invalid, setInvalid] = useState(false)

  const cap = pace?.cap ?? null

  function startEditing() {
    const prefill = cap ? Number(cap.cap) : typicalMonthlySpending
    setDraft(prefill != null ? formatAmountInput(prefill, locale) : '')
    setInvalid(false)
    setEditing(true)
  }

  function commit() {
    const parsed = parseAmountInput(draft, locale)
    if (parsed === null || parsed <= 0) {
      setInvalid(true)
      return
    }
    onSetCap(parsed)
    setEditing(false)
  }

  return (
    <div className="bg-card rounded-xl border border-border shadow-sm overflow-hidden mb-5">
      <div className="px-4 sm:px-5 py-4 border-b border-border flex items-center justify-between gap-2">
        <p className="text-sm font-semibold text-foreground">{t('budgetsPace.cap.title')}</p>
        {cap && !editing && (
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={startEditing}
              className="text-xs font-medium text-primary hover:underline cursor-pointer"
            >
              {t('common.edit')}
            </button>
            <button
              type="button"
              onClick={onClearCap}
              disabled={isSaving}
              className="text-xs font-medium text-muted-foreground hover:text-rose-500 cursor-pointer disabled:opacity-50"
            >
              {t('budgetsPace.cap.clear')}
            </button>
          </div>
        )}
      </div>

      <div className="p-4 sm:p-5 space-y-3">
        {isLoading ? (
          <Skeleton className="h-10 w-full" role="status" aria-label={t('common.loading')} />
        ) : editing ? (
          <div className="flex flex-wrap items-end gap-2">
            <div className="space-y-1.5">
              <Label htmlFor="budgets-cap-input">{t('budgetsPace.cap.inputLabel')}</Label>
              <Input
                id="budgets-cap-input"
                type="text"
                inputMode="decimal"
                value={draft}
                aria-invalid={invalid || undefined}
                onChange={(e) => {
                  setDraft(e.target.value)
                  setInvalid(false)
                }}
                autoFocus
              />
            </div>
            <Button size="sm" onClick={commit} disabled={isSaving}>
              {t('common.save')}
            </Button>
            <Button size="sm" variant="outline" onClick={() => setEditing(false)} disabled={isSaving}>
              {t('common.cancel')}
            </Button>
          </div>
        ) : cap ? (
          <>
            <BulletBar
              value={Number(cap.spent)}
              target={Number(cap.cap)}
              marker={Number(cap.expected_to_date)}
              markerLabel={t('budgetsPace.marker')}
              currency={currency}
            />
            <div className="flex flex-wrap items-center gap-3">
              <PaceBadge status={cap.status} />
              <span className="text-xs text-muted-foreground">
                {t('budgetsPace.leftToSpend', { amount: mask(formatMoney(Number(cap.left_to_spend), currency, { locale })) })}
              </span>
              {cap.left_per_day != null && (
                <span className="text-xs text-muted-foreground">
                  {t('budgetsPace.leftPerDay', { amount: mask(formatMoney(Number(cap.left_per_day), currency, { locale })) })}
                </span>
              )}
            </div>
          </>
        ) : (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-muted-foreground">{t('budgetsPace.cap.prompt')}</p>
            <Button size="sm" onClick={startEditing}>
              {t('budgetsPace.cap.setAction')}
            </Button>
          </div>
        )}
      </div>
    </div>
  )
}
