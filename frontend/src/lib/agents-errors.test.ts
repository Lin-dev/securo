import { describe, expect, it } from 'vitest'
import { KNOWN_CHAT_ERRORS, formatChatError, isKnownChatError } from './agents-errors'

const t = (k: string) => k

describe('formatChatError', () => {
  it('maps every known code to its translation key', () => {
    for (const code of KNOWN_CHAT_ERRORS) {
      expect(formatChatError(t, code)).toContain(`agents.chat.errors.${code}`)
    }
  })

  it('keeps the raw detail only for codes where it adds information', () => {
    expect(formatChatError(t, 'unavailable', 'Ollama 500: boom')).toBe('agents.chat.errors.unavailable — Ollama 500: boom')
    expect(formatChatError(t, 'config', 'no model')).toBe('agents.chat.errors.config — no model')
    expect(formatChatError(t, 'auth', 'bad key')).toBe('agents.chat.errors.auth — bad key')
    expect(formatChatError(t, 'unknown', 'kaboom')).toBe('agents.chat.errors.unknown — kaboom')
  })

  it('drops the detail for codes whose headline says it all', () => {
    expect(formatChatError(t, 'max_iterations', 'Agent reached its tool-call limit.')).toBe('agents.chat.errors.max_iterations')
    expect(formatChatError(t, 'rate_limit', '429')).toBe('agents.chat.errors.rate_limit')
    expect(formatChatError(t, 'empty_response', '')).toBe('agents.chat.errors.empty_response')
    expect(formatChatError(t, 'not_supported')).toBe('agents.chat.errors.not_supported')
  })

  it('falls back to the raw code and message for unknown codes', () => {
    expect(formatChatError(t, '503', 'Service Unavailable')).toBe('503: Service Unavailable')
    expect(formatChatError(t, 'parse', '{bad')).toBe('parse: {bad')
    expect(formatChatError(t, undefined, 'oops')).toBe('error: oops')
    expect(formatChatError(t, undefined, undefined)).toBe('error: ')
  })

  it('exposes the guard', () => {
    expect(isKnownChatError('auth')).toBe(true)
    expect(isKnownChatError('AUTH')).toBe(false)
    expect(isKnownChatError(42)).toBe(false)
  })
})
