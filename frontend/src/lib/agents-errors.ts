/**
 * Human-readable chat errors. The backend streams `error` events with a
 * stable `error_code`; known codes get a translated headline, and only the
 * codes whose raw message adds something (a URL, a status line) keep it.
 */
export const KNOWN_CHAT_ERRORS = [
  'max_iterations',
  'unavailable',
  'config',
  'empty_response',
  'auth',
  'rate_limit',
  'not_supported',
  'unknown',
] as const

export type KnownChatError = (typeof KNOWN_CHAT_ERRORS)[number]

const WITH_DETAIL: ReadonlySet<string> = new Set(['unavailable', 'config', 'auth', 'unknown'])

type Translate = (key: string) => string

export function isKnownChatError(code: unknown): code is KnownChatError {
  return typeof code === 'string' && (KNOWN_CHAT_ERRORS as readonly string[]).includes(code)
}

export function formatChatError(t: Translate, code?: string, message?: string): string {
  if (!isKnownChatError(code)) return `${code || 'error'}: ${message || ''}`
  const headline = t(`agents.chat.errors.${code}`)
  const detail = (message || '').trim()
  return WITH_DETAIL.has(code) && detail ? `${headline} — ${detail}` : headline
}
