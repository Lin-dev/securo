import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { formatAmountInput, parseAmountInput } from '@/lib/format'
import type { WealthSettingSource, WealthSettingsUpdate } from '@/types/wealth'

import { SettingsCard } from './settings-card'
import { SourceBadge } from './source-badge'

// Plan P5/P3 bounds (mirrors backend/app/schemas/wealth.py's ProjectionSettings
// and buffer_months field constraints).
const RETURN_MIN = -5
const RETURN_MAX = 15
const WITHDRAWAL_MAX = 10
const BUFFER_MIN = 0
const BUFFER_MAX = 24

interface AssumptionsCardProps {
  returnNominal: number
  withdrawalRate: number
  bufferMonths: number
  projectionSource: WealthSettingSource
  bufferSource: WealthSettingSource
  onSave: (body: WealthSettingsUpdate) => void
  disabled?: boolean
}

/** Expected return, withdrawal rate and cash buffer — the three inputs the
 *  projection and lever engines (plan P5) read. Saved on blur, one request
 *  per field, never per keystroke. */
export function AssumptionsCard({
  returnNominal,
  withdrawalRate,
  bufferMonths,
  projectionSource,
  bufferSource,
  onSave,
  disabled,
}: AssumptionsCardProps) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()

  // `undefined` means "nothing typed yet" — the field shows the server value.
  // Once the person types, the draft owns the field until it is committed
  // (valid) or the field blurs invalid, in which case it stays so the error
  // sits next to what they actually typed.
  const [returnDraft, setReturnDraft] = useState<string | undefined>(undefined)
  const [returnInvalid, setReturnInvalid] = useState(false)
  const [withdrawalDraft, setWithdrawalDraft] = useState<string | undefined>(undefined)
  const [withdrawalInvalid, setWithdrawalInvalid] = useState(false)
  const [bufferDraft, setBufferDraft] = useState<string | undefined>(undefined)
  const [bufferInvalid, setBufferInvalid] = useState(false)

  const returnValue = returnDraft ?? formatAmountInput(returnNominal * 100, locale)
  const withdrawalValue = withdrawalDraft ?? formatAmountInput(withdrawalRate * 100, locale)
  const bufferValue = bufferDraft ?? formatAmountInput(bufferMonths, locale)

  function commitReturn() {
    if (returnDraft === undefined) return
    const parsed = parseAmountInput(returnDraft, locale)
    if (parsed === null || parsed < RETURN_MIN || parsed > RETURN_MAX) {
      setReturnInvalid(true)
      return
    }
    setReturnInvalid(false)
    setReturnDraft(undefined)
    onSave({ projection: { return_nominal: parsed / 100, withdrawal_rate: withdrawalRate } })
  }

  function commitWithdrawal() {
    if (withdrawalDraft === undefined) return
    const parsed = parseAmountInput(withdrawalDraft, locale)
    if (parsed === null || parsed <= 0 || parsed > WITHDRAWAL_MAX) {
      setWithdrawalInvalid(true)
      return
    }
    setWithdrawalInvalid(false)
    setWithdrawalDraft(undefined)
    onSave({ projection: { return_nominal: returnNominal, withdrawal_rate: parsed / 100 } })
  }

  function commitBuffer() {
    if (bufferDraft === undefined) return
    const parsed = parseAmountInput(bufferDraft, locale)
    if (parsed === null || parsed < BUFFER_MIN || parsed > BUFFER_MAX) {
      setBufferInvalid(true)
      return
    }
    setBufferInvalid(false)
    setBufferDraft(undefined)
    onSave({ buffer_months: parsed })
  }

  return (
    <SettingsCard title={t('wealthSettings.assumptions.title')}>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <Label htmlFor="wealth-settings-return">{t('wealthSettings.assumptions.returnLabel')}</Label>
            <SourceBadge source={projectionSource} />
          </div>
          <div className="relative">
            <Input
              id="wealth-settings-return"
              type="text"
              inputMode="decimal"
              className="pr-7"
              value={returnValue}
              disabled={disabled}
              aria-invalid={returnInvalid || undefined}
              aria-describedby={returnInvalid ? 'wealth-settings-return-error' : 'wealth-settings-return-help'}
              onChange={(e) => {
                setReturnDraft(e.target.value)
                setReturnInvalid(false)
              }}
              onBlur={commitReturn}
            />
            <span
              aria-hidden="true"
              className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-sm text-muted-foreground"
            >
              %
            </span>
          </div>
          {returnInvalid ? (
            <p id="wealth-settings-return-error" role="alert" className="text-xs text-rose-500">
              {t('wealthSettings.assumptions.returnError')}
            </p>
          ) : (
            <p id="wealth-settings-return-help" className="text-xs text-muted-foreground">
              {t('wealthSettings.assumptions.returnHelp')}
            </p>
          )}
        </div>

        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <Label htmlFor="wealth-settings-withdrawal">{t('wealthSettings.assumptions.withdrawalLabel')}</Label>
            <SourceBadge source={projectionSource} />
          </div>
          <div className="relative">
            <Input
              id="wealth-settings-withdrawal"
              type="text"
              inputMode="decimal"
              className="pr-7"
              value={withdrawalValue}
              disabled={disabled}
              aria-invalid={withdrawalInvalid || undefined}
              aria-describedby={
                withdrawalInvalid ? 'wealth-settings-withdrawal-error' : 'wealth-settings-withdrawal-help'
              }
              onChange={(e) => {
                setWithdrawalDraft(e.target.value)
                setWithdrawalInvalid(false)
              }}
              onBlur={commitWithdrawal}
            />
            <span
              aria-hidden="true"
              className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-sm text-muted-foreground"
            >
              %
            </span>
          </div>
          {withdrawalInvalid ? (
            <p id="wealth-settings-withdrawal-error" role="alert" className="text-xs text-rose-500">
              {t('wealthSettings.assumptions.withdrawalError')}
            </p>
          ) : (
            <p id="wealth-settings-withdrawal-help" className="text-xs text-muted-foreground">
              {t('wealthSettings.assumptions.withdrawalHelp')}
            </p>
          )}
        </div>

        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <Label htmlFor="wealth-settings-buffer">{t('wealthSettings.assumptions.bufferLabel')}</Label>
            <SourceBadge source={bufferSource} />
          </div>
          <Input
            id="wealth-settings-buffer"
            type="text"
            inputMode="decimal"
            value={bufferValue}
            disabled={disabled}
            aria-invalid={bufferInvalid || undefined}
            aria-describedby={bufferInvalid ? 'wealth-settings-buffer-error' : 'wealth-settings-buffer-help'}
            onChange={(e) => {
              setBufferDraft(e.target.value)
              setBufferInvalid(false)
            }}
            onBlur={commitBuffer}
          />
          {bufferInvalid ? (
            <p id="wealth-settings-buffer-error" role="alert" className="text-xs text-rose-500">
              {t('wealthSettings.assumptions.bufferError')}
            </p>
          ) : (
            <p id="wealth-settings-buffer-help" className="text-xs text-muted-foreground">
              {t('wealthSettings.assumptions.bufferHelp')}
            </p>
          )}
        </div>
      </div>
    </SettingsCard>
  )
}
