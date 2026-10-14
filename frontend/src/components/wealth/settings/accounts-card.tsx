import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { formatAmountInput, parseAmountInput } from '@/lib/format'
import { WEALTH_BUCKETS } from '@/types/wealth'
import type { WealthAccountSettingRow, WealthBucket, WealthSettingsUpdate } from '@/types/wealth'

import { SettingsCard } from './settings-card'
import { SourceBadge } from './source-badge'

// Field(ge=0, le=100) in backend/app/schemas/wealth.py's AccountAnnotation.
const APR_MAX = 100

interface AccountsCardProps {
  rows: WealthAccountSettingRow[]
  onSave: (body: WealthSettingsUpdate) => void
  disabled?: boolean
}

/** The account bucket table (plan P3): every account's effective bucket,
 *  liquidity and (for card debt) APR, plus the overrides left over from an
 *  account that no longer exists. */
export function AccountsCard({ rows, onSave, disabled }: AccountsCardProps) {
  const { t } = useTranslation()
  const activeRows = rows.filter((row) => !row.orphaned)
  const orphanedRows = rows.filter((row) => row.orphaned)

  function bucketLabel(bucket: WealthBucket) {
    return t(`wealthSettings.buckets.${bucket}`)
  }

  // The backend merges `accounts` per account key (plan P6), replacing
  // whatever was stored under that key — so every change resends the row's
  // full effective annotation, never just the one field that changed, or an
  // unrelated earlier override on the same account would be dropped.
  function saveAccount(
    row: WealthAccountSettingRow,
    next: { bucket?: WealthBucket; liquid?: boolean; apr?: number | null },
  ) {
    const bucket = next.bucket ?? row.bucket
    onSave({
      accounts: {
        [row.override_key]: {
          bucket,
          liquid: next.liquid ?? row.liquid,
          apr: bucket === 'card_debt' ? (next.apr !== undefined ? next.apr : row.apr) : null,
        },
      },
    })
  }

  function removeOverride(row: WealthAccountSettingRow) {
    onSave({ accounts: { [row.override_key]: null } })
  }

  return (
    <SettingsCard title={t('wealthSettings.accounts.title')}>
      {activeRows.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t('wealthSettings.accounts.empty')}</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t('wealthSettings.accounts.account')}</TableHead>
              <TableHead>{t('wealthSettings.accounts.bucket')}</TableHead>
              <TableHead>{t('wealthSettings.accounts.liquid')}</TableHead>
              <TableHead>{t('wealthSettings.accounts.apr')}</TableHead>
              <TableHead>{t('wealthSettings.accounts.source')}</TableHead>
              <TableHead className="sr-only">{t('wealthSettings.accounts.reset')}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {activeRows.map((row) => (
              <AccountRow
                key={row.override_key}
                row={row}
                disabled={disabled}
                bucketLabel={bucketLabel}
                onChange={(next) => saveAccount(row, next)}
                onReset={() => removeOverride(row)}
              />
            ))}
          </TableBody>
        </Table>
      )}

      {orphanedRows.length > 0 && (
        <div className="space-y-2 border-t border-border/40 pt-4">
          <h3 className="text-sm font-medium text-foreground">{t('wealthSettings.accounts.orphanedTitle')}</h3>
          <ul className="space-y-2">
            {orphanedRows.map((row) => (
              <li key={row.override_key} className="flex items-center justify-between gap-3 text-sm">
                <span className="text-muted-foreground">{row.name}</span>
                <Button variant="outline" size="sm" disabled={disabled} onClick={() => removeOverride(row)}>
                  {t('common.remove')}
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </SettingsCard>
  )
}

interface AccountRowProps {
  row: WealthAccountSettingRow
  disabled?: boolean
  bucketLabel: (bucket: WealthBucket) => string
  onChange: (next: { bucket?: WealthBucket; liquid?: boolean; apr?: number | null }) => void
  onReset: () => void
}

function AccountRow({ row, disabled, bucketLabel, onChange, onReset }: AccountRowProps) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const [aprDraft, setAprDraft] = useState<string | undefined>(undefined)
  const [aprInvalid, setAprInvalid] = useState(false)

  const aprValue = aprDraft ?? (row.apr != null ? formatAmountInput(row.apr, locale) : '')

  function commitApr() {
    if (aprDraft === undefined) return
    if (aprDraft.trim() === '') {
      setAprInvalid(false)
      setAprDraft(undefined)
      onChange({ apr: null })
      return
    }
    const parsed = parseAmountInput(aprDraft, locale)
    if (parsed === null || parsed < 0 || parsed > APR_MAX) {
      setAprInvalid(true)
      return
    }
    setAprInvalid(false)
    setAprDraft(undefined)
    onChange({ apr: parsed })
  }

  return (
    <TableRow>
      <TableCell>
        <div className="font-medium text-foreground">{row.name}</div>
        {row.institution && <div className="text-xs text-muted-foreground">{row.institution}</div>}
      </TableCell>
      <TableCell>
        <Select
          value={row.bucket}
          onValueChange={(value) => onChange({ bucket: value as WealthBucket })}
          disabled={disabled}
        >
          <SelectTrigger
            className="w-48"
            aria-label={t('wealthSettings.accounts.bucketFor', { name: row.name })}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {WEALTH_BUCKETS.map((bucket) => (
              <SelectItem key={bucket} value={bucket}>
                {bucket === row.default_bucket
                  ? `${bucketLabel(bucket)}${t('wealthSettings.accounts.defaultSuffix')}`
                  : bucketLabel(bucket)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </TableCell>
      <TableCell>
        <span id={`wealth-liquid-label-${row.override_key}`} className="sr-only">
          {t('wealthSettings.accounts.liquidFor', { name: row.name })}
        </span>
        <Switch
          checked={row.liquid}
          disabled={disabled}
          aria-labelledby={`wealth-liquid-label-${row.override_key}`}
          onCheckedChange={(checked) => onChange({ liquid: checked })}
        />
      </TableCell>
      <TableCell>
        {row.bucket === 'card_debt' ? (
          <div className="w-24">
            <label htmlFor={`wealth-apr-${row.override_key}`} className="sr-only">
              {t('wealthSettings.accounts.aprFor', { name: row.name })}
            </label>
            <Input
              id={`wealth-apr-${row.override_key}`}
              type="text"
              inputMode="decimal"
              className="h-8"
              value={aprValue}
              disabled={disabled}
              aria-invalid={aprInvalid || undefined}
              aria-describedby={aprInvalid ? `wealth-apr-error-${row.override_key}` : undefined}
              onChange={(e) => {
                setAprDraft(e.target.value)
                setAprInvalid(false)
              }}
              onBlur={commitApr}
            />
            {aprInvalid && (
              <p id={`wealth-apr-error-${row.override_key}`} role="alert" className="text-xs text-rose-500">
                {t('wealthSettings.accounts.aprError')}
              </p>
            )}
          </div>
        ) : (
          <span className="text-muted-foreground">&mdash;</span>
        )}
      </TableCell>
      <TableCell>
        <SourceBadge source={row.source} />
      </TableCell>
      <TableCell>
        {row.source === 'user' && (
          <Button variant="ghost" size="sm" disabled={disabled} onClick={onReset}>
            {t('wealthSettings.accounts.reset')}
          </Button>
        )}
      </TableCell>
    </TableRow>
  )
}
