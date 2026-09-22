import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  PLAID_LINK_SCRIPT_URL,
  PLAID_SESSION_KEY,
  apiErrorMessage,
  clearPlaidSession,
  isPlaidOAuthReturn,
  loadPlaidLink,
  plaidExitMessage,
  readPlaidSession,
  resetPlaidLinkLoader,
  savePlaidSession,
} from './plaid-link'

const scripts = () => Array.from(document.querySelectorAll<HTMLScriptElement>(`script[src="${PLAID_LINK_SCRIPT_URL}"]`))

describe('loadPlaidLink', () => {
  beforeEach(() => {
    resetPlaidLinkLoader()
    delete window.Plaid
    scripts().forEach((s) => s.remove())
  })
  afterEach(() => {
    delete window.Plaid
  })

  it('resolves at once when Plaid is already on the page', async () => {
    const plaid = { create: vi.fn() }
    window.Plaid = plaid
    await expect(loadPlaidLink()).resolves.toBe(plaid)
    expect(scripts()).toHaveLength(0)
  })

  it('injects one script for concurrent callers and resolves them all on load', async () => {
    const first = loadPlaidLink()
    const second = loadPlaidLink()
    expect(scripts()).toHaveLength(1)
    const plaid = { create: vi.fn() }
    window.Plaid = plaid
    scripts()[0].dispatchEvent(new Event('load'))
    await expect(first).resolves.toBe(plaid)
    await expect(second).resolves.toBe(plaid)
  })

  it('rejects on a script error and lets the next call try again', async () => {
    const attempt = loadPlaidLink()
    scripts()[0].dispatchEvent(new Event('error'))
    await expect(attempt).rejects.toThrow(/failed to load/)
    expect(scripts()).toHaveLength(0)
    const retry = loadPlaidLink()
    expect(scripts()).toHaveLength(1)
    window.Plaid = { create: vi.fn() }
    scripts()[0].dispatchEvent(new Event('load'))
    await expect(retry).resolves.toBeDefined()
  })
})

describe('plaid session', () => {
  beforeEach(() => sessionStorage.clear())

  it('round-trips the in-flight link session', () => {
    savePlaidSession({ linkToken: 'link-1', provider: 'plaid', reconnectConnectionId: 'c1', syncAssets: false })
    const session = readPlaidSession()
    expect(session).toMatchObject({ linkToken: 'link-1', provider: 'plaid', reconnectConnectionId: 'c1', syncAssets: false })
    expect(session?.startedAt).toBeGreaterThan(0)
    clearPlaidSession()
    expect(readPlaidSession()).toBeNull()
  })

  it('ignores garbage and missing tokens', () => {
    sessionStorage.setItem(PLAID_SESSION_KEY, 'not json')
    expect(readPlaidSession()).toBeNull()
    sessionStorage.setItem(PLAID_SESSION_KEY, JSON.stringify({ provider: 'plaid' }))
    expect(readPlaidSession()).toBeNull()
    sessionStorage.setItem(PLAID_SESSION_KEY, JSON.stringify({ linkToken: 'x' }))
    expect(readPlaidSession()).toMatchObject({ linkToken: 'x', provider: 'plaid', reconnectConnectionId: undefined })
  })

  it('recognises the OAuth return URL', () => {
    expect(isPlaidOAuthReturn('?oauth_state_id=abc')).toBe(true)
    expect(isPlaidOAuthReturn('?other=1')).toBe(false)
    expect(isPlaidOAuthReturn('')).toBe(false)
  })
})

describe('error text', () => {
  it('prefers the display message and stays quiet on a plain close', () => {
    expect(plaidExitMessage(null)).toBeNull()
    expect(plaidExitMessage({ error_code: 'INSTITUTION_DOWN' })).toBe('INSTITUTION_DOWN')
    expect(plaidExitMessage({ error_code: 'X', error_message: 'msg', display_message: 'shown' })).toBe('shown')
  })

  it('unwraps string and object details from axios errors', () => {
    expect(apiErrorMessage({ response: { data: { detail: 'bad token' } } })).toBe('bad token')
    expect(apiErrorMessage({ response: { data: { detail: { message: 'nope', code: 'x' } } } })).toBe('nope')
    expect(apiErrorMessage(new Error('boom'))).toBeNull()
    expect(apiErrorMessage(null)).toBeNull()
  })
})
