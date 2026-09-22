import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Building2 } from 'lucide-react'
import { connections } from '@/lib/api'
import { invalidateFinancialQueries } from '@/lib/invalidate-queries'
import {
  apiErrorMessage,
  clearPlaidSession,
  isPlaidOAuthReturn,
  loadPlaidLink,
  plaidExitMessage,
  readPlaidSession,
  type PlaidLinkHandler,
} from '@/lib/plaid-link'
import { Button } from '@/components/ui/button'

/**
 * Plaid's OAuth return leg. An OAuth bank sends the browser back here with an
 * `oauth_state_id`; Link must be re-created with the same link token that
 * started the flow (parked in sessionStorage) and the full return URL, after
 * which the normal success/exit handling finishes the connection.
 */
export default function PlaidOAuthPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [session] = useState(() => readPlaidSession())
  const [isReturn] = useState(() => isPlaidOAuthReturn(window.location.search))
  const [failed, setFailed] = useState(false)
  const startedRef = useRef(false)
  const missing = !session || !isReturn

  useEffect(() => {
    if (missing || startedRef.current) return
    startedRef.current = true
    let handler: PlaidLinkHandler | null = null
    const leave = () => navigate('/accounts', { replace: true })

    const complete = async (publicToken: string | null, exitMessage: string | null) => {
      try {
        if (publicToken === null) {
          if (exitMessage) toast.error(exitMessage)
          return
        }
        if (session.reconnectConnectionId) {
          await connections.sync(session.reconnectConnectionId)
        } else {
          await connections.handleCallback(
            publicToken,
            session.provider,
            undefined,
            session.syncAssets === undefined ? undefined : { sync_assets: session.syncAssets },
          )
        }
        invalidateFinancialQueries(queryClient)
        queryClient.invalidateQueries({ queryKey: ['connections'] })
        toast.success(t(session.reconnectConnectionId ? 'accounts.reconnected' : 'accounts.connected'))
      } catch (err) {
        toast.error(apiErrorMessage(err) ?? t('accounts.connectError'))
      } finally {
        clearPlaidSession()
        leave()
      }
    }

    loadPlaidLink()
      .then((Plaid) => {
        handler = Plaid.create({
          token: session.linkToken,
          receivedRedirectUri: window.location.href,
          onSuccess: (publicToken) => { void complete(publicToken, null) },
          onExit: (error) => { void complete(null, plaidExitMessage(error)) },
        })
        handler.open()
      })
      .catch(() => setFailed(true))

    return () => { handler?.destroy?.() }
  }, [missing, session, navigate, queryClient, t])

  const title = missing
    ? t('accounts.plaidConnect.returnMissing')
    : failed
      ? t('accounts.plaidConnect.failed')
      : t('accounts.plaidConnect.finishing')
  const body = missing
    ? t('accounts.plaidConnect.returnMissingDesc')
    : failed
      ? t('accounts.connectError')
      : t('accounts.oauthCallback.dontClose')

  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-4 px-4 py-16 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted text-muted-foreground">
        <Building2 size={22} />
      </div>
      <h1 className="text-lg font-semibold text-foreground">{title}</h1>
      <p className="text-sm text-muted-foreground">{body}</p>
      {(missing || failed) && (
        <Button onClick={() => { clearPlaidSession(); navigate('/accounts', { replace: true }) }}>
          {t('accounts.plaidConnect.backToAccounts')}
        </Button>
      )}
    </div>
  )
}
