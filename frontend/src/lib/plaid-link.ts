// Plaid Link glue: load Plaid's script on demand, remember the in-flight
// session across an OAuth redirect, and unwrap API errors for toasts.
//
// Plaid Link is served from Plaid's CDN and attaches `window.Plaid`; we load
// it lazily (only when a Plaid dialog opens) instead of bundling an npm
// package. OAuth banks send the browser to our registered redirect URI in a
// fresh page load, and Link must then be re-created with the SAME link
// token plus the full return URL, so the token is parked in sessionStorage
// for the duration of the flow.

export const PLAID_LINK_SCRIPT_URL = 'https://cdn.plaid.com/link/v2/stable/link-initialize.js'
export const PLAID_SESSION_KEY = 'securo:plaid:link'

export interface PlaidLinkSuccessMetadata {
  institution?: { name?: string; institution_id?: string } | null
  accounts?: unknown[]
  link_session_id?: string
}

export interface PlaidLinkExitError {
  error_type?: string
  error_code?: string
  error_message?: string
  display_message?: string | null
}

export interface PlaidLinkHandler {
  open: () => void
  exit: (options?: { force?: boolean }) => void
  destroy: () => void
}

export interface PlaidLinkConfig {
  token: string
  receivedRedirectUri?: string
  onSuccess: (publicToken: string, metadata: PlaidLinkSuccessMetadata) => void
  onExit?: (error: PlaidLinkExitError | null, metadata: unknown) => void
  onEvent?: (eventName: string, metadata: unknown) => void
}

export interface PlaidGlobal {
  create: (config: PlaidLinkConfig) => PlaidLinkHandler
}

declare global {
  interface Window {
    Plaid?: PlaidGlobal
  }
}

let loader: Promise<PlaidGlobal> | null = null

/** Resolve `window.Plaid`, injecting the script tag once if needed. */
export function loadPlaidLink(doc: Document = document): Promise<PlaidGlobal> {
  if (window.Plaid) return Promise.resolve(window.Plaid)
  if (loader) return loader
  loader = new Promise<PlaidGlobal>((resolve, reject) => {
    const existing = doc.querySelector<HTMLScriptElement>(`script[src="${PLAID_LINK_SCRIPT_URL}"]`)
    const script = existing ?? doc.createElement('script')
    const onLoad = () => {
      if (window.Plaid) {
        resolve(window.Plaid)
      } else {
        loader = null
        reject(new Error('Plaid Link loaded but did not initialise'))
      }
    }
    const onError = () => {
      loader = null
      script.remove()
      reject(new Error('Plaid Link script failed to load'))
    }
    script.addEventListener('load', onLoad, { once: true })
    script.addEventListener('error', onError, { once: true })
    if (!existing) {
      script.src = PLAID_LINK_SCRIPT_URL
      script.async = true
      doc.head.appendChild(script)
    }
  })
  return loader
}

/** Test hook: forget a previous load attempt. */
export function resetPlaidLinkLoader(): void {
  loader = null
}

export interface PlaidLinkSession {
  linkToken: string
  provider: string
  reconnectConnectionId?: string
  syncAssets?: boolean
  startedAt: number
}

export function savePlaidSession(session: Omit<PlaidLinkSession, 'startedAt'>): void {
  try {
    sessionStorage.setItem(PLAID_SESSION_KEY, JSON.stringify({ ...session, startedAt: Date.now() }))
  } catch {
    // storage unavailable: the non-OAuth path still works, OAuth banks will not
  }
}

export function readPlaidSession(): PlaidLinkSession | null {
  try {
    const raw = sessionStorage.getItem(PLAID_SESSION_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<PlaidLinkSession>
    if (typeof parsed.linkToken !== 'string' || !parsed.linkToken) return null
    return {
      linkToken: parsed.linkToken,
      provider: typeof parsed.provider === 'string' && parsed.provider ? parsed.provider : 'plaid',
      reconnectConnectionId: typeof parsed.reconnectConnectionId === 'string' ? parsed.reconnectConnectionId : undefined,
      syncAssets: typeof parsed.syncAssets === 'boolean' ? parsed.syncAssets : undefined,
      startedAt: typeof parsed.startedAt === 'number' ? parsed.startedAt : 0,
    }
  } catch {
    return null
  }
}

export function clearPlaidSession(): void {
  try {
    sessionStorage.removeItem(PLAID_SESSION_KEY)
  } catch {
    // ignore
  }
}

/** True when the current URL is Plaid's OAuth return (carries `oauth_state_id`). */
export function isPlaidOAuthReturn(search: string): boolean {
  return new URLSearchParams(search).has('oauth_state_id')
}

/** Human text for a Link exit error, or null when the user simply closed it. */
export function plaidExitMessage(error: PlaidLinkExitError | null | undefined): string | null {
  if (!error) return null
  return error.display_message || error.error_message || error.error_code || null
}

/** Unwrap the backend's `detail` (string or `{message}`) from an axios error. */
export function apiErrorMessage(err: unknown): string | null {
  if (typeof err !== 'object' || err === null) return null
  const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object') {
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string' && message.trim()) return message
  }
  return null
}
