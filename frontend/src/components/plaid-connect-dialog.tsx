import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { connections } from '@/lib/api'
import { invalidateFinancialQueries } from '@/lib/invalidate-queries'
import {
  apiErrorMessage,
  clearPlaidSession,
  loadPlaidLink,
  plaidExitMessage,
  savePlaidSession,
  type PlaidLinkHandler,
} from '@/lib/plaid-link'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'

interface PlaidConnectDialogProps {
  open: boolean
  onClose: () => void
  provider?: string
  supportsAssetSync?: boolean
  /** Set for reconnects: Link opens in update mode and a sync follows. */
  reconnectConnectionId?: string
}

/**
 * Plaid Link flow. Mirrors the Pluggy widget dialog: an optional sync-options
 * step, then the provider's own UI. Once Link is open this component renders
 * nothing, so Radix's focus trap never fights Plaid's iframe. A reconnect
 * never posts a public token back: update mode keeps the same access token,
 * so a plain sync is the right follow-up.
 */
export function PlaidConnectDialog({
  open,
  onClose,
  provider = 'plaid',
  supportsAssetSync = false,
  reconnectConnectionId,
}: PlaidConnectDialogProps) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [syncAssets, setSyncAssets] = useState(true)
  const [optionsConfirmed, setOptionsConfirmed] = useState(false)
  const handlerRef = useRef<PlaidLinkHandler | null>(null)
  const startedRef = useRef(false)
  // Latest callbacks/values for the long-lived Link callbacks, so a parent
  // re-render while Link is open never restarts or cancels the flow.
  const latest = useRef({ onClose, syncAssets, t, queryClient })
  useEffect(() => {
    latest.current = { onClose, syncAssets, t, queryClient }
  })

  const needsInitialOptions = !reconnectConnectionId && supportsAssetSync
  const showOptions = open && needsInitialOptions && !optionsConfirmed
  const shouldStart = open && (!needsInitialOptions || optionsConfirmed)

  const finish = () => {
    handlerRef.current?.destroy?.()
    handlerRef.current = null
    startedRef.current = false
    setOptionsConfirmed(false)
    setSyncAssets(true)
    latest.current.onClose()
  }

  useEffect(() => {
    if (!shouldStart) {
      // Closed from outside while Link was up: tear it down quietly.
      handlerRef.current?.destroy?.()
      handlerRef.current = null
      startedRef.current = false
      return
    }
    if (startedRef.current) return
    startedRef.current = true
    let cancelled = false

    const complete = async (publicToken: string | null, exitMessage: string | null) => {
      const { t: tr, queryClient: qc, syncAssets: wantAssets } = latest.current
      try {
        if (publicToken === null) {
          if (exitMessage) toast.error(exitMessage)
          return
        }
        if (reconnectConnectionId) {
          await connections.sync(reconnectConnectionId)
        } else {
          await connections.handleCallback(
            publicToken,
            provider,
            undefined,
            supportsAssetSync ? { sync_assets: wantAssets } : undefined,
          )
        }
        invalidateFinancialQueries(qc)
        qc.invalidateQueries({ queryKey: ['connections'] })
        toast.success(tr(reconnectConnectionId ? 'accounts.reconnected' : 'accounts.connected'))
      } catch (err) {
        toast.error(apiErrorMessage(err) ?? tr('accounts.connectError'))
      } finally {
        clearPlaidSession()
        handlerRef.current?.destroy?.()
        handlerRef.current = null
        startedRef.current = false
        setOptionsConfirmed(false)
        setSyncAssets(true)
        latest.current.onClose()
      }
    }

    const start = async () => {
      try {
        const linkToken = reconnectConnectionId
          ? await connections.getReconnectToken(reconnectConnectionId)
          : await connections.getConnectToken(provider)
        if (cancelled) return
        savePlaidSession({
          linkToken,
          provider,
          reconnectConnectionId,
          syncAssets: supportsAssetSync ? latest.current.syncAssets : undefined,
        })
        const Plaid = await loadPlaidLink()
        if (cancelled) return
        const handler = Plaid.create({
          token: linkToken,
          onSuccess: (publicToken) => { void complete(publicToken, null) },
          onExit: (error) => { void complete(null, plaidExitMessage(error)) },
        })
        handlerRef.current = handler
        handler.open()
      } catch {
        if (cancelled) return
        clearPlaidSession()
        toast.error(latest.current.t('accounts.connectError'))
        startedRef.current = false
        latest.current.onClose()
      }
    }
    void start()

    return () => { cancelled = true }
  }, [shouldStart, provider, supportsAssetSync, reconnectConnectionId])

  if (showOptions) {
    return (
      <Dialog open={open} onOpenChange={(v) => !v && finish()}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t('connections.initialSyncSettings')}</DialogTitle>
            <p className="text-sm text-muted-foreground">{t('connections.initialSyncSettingsDesc')}</p>
          </DialogHeader>
          <div className="flex items-start justify-between gap-4 rounded-lg border border-border p-3">
            <div className="space-y-1">
              <Label htmlFor="plaid-initial-sync-assets">{t('connections.syncAssets')}</Label>
              <p className="text-xs text-muted-foreground">{t('connections.syncAssetsHint')}</p>
            </div>
            <input
              id="plaid-initial-sync-assets"
              type="checkbox"
              checked={syncAssets}
              onChange={(e) => setSyncAssets(e.target.checked)}
              className="mt-1 h-4 w-4 rounded border-border text-primary focus:ring-primary"
            />
          </div>
          <p className="text-xs text-muted-foreground">{t('accounts.plaidConnect.description')}</p>
          <DialogFooter>
            <Button variant="outline" onClick={finish}>{t('common.cancel')}</Button>
            <Button onClick={() => setOptionsConfirmed(true)}>{t('connections.continueToConnector')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    )
  }

  // Link draws its own modal; nothing of ours should sit on top of it.
  return null
}
