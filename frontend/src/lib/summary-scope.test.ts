import { describe, expect, it } from 'vitest'
import {
  SUMMARY_SCOPES,
  SUMMARY_SCOPE_PARAM,
  parseSummaryScope,
  summaryScopeLabelKey,
  summaryScopeValue,
  toggleSummaryScope,
} from './summary-scope'

describe('summary scope', () => {
  it('uses the API payload keys and a stable param name', () => {
    expect([...SUMMARY_SCOPES]).toEqual(['income', 'expense', 'net', 'excluded', 'invested'])
    expect(SUMMARY_SCOPE_PARAM).toBe('summary_scope')
  })

  it('parses only known scopes from the URL', () => {
    expect(parseSummaryScope('income')).toBe('income')
    expect(parseSummaryScope('invested')).toBe('invested')
    expect(parseSummaryScope('expenses')).toBeNull()
    expect(parseSummaryScope('')).toBeNull()
    expect(parseSummaryScope(null)).toBeNull()
    expect(parseSummaryScope(undefined)).toBeNull()
  })

  it('toggles as a single-select control', () => {
    expect(toggleSummaryScope(null, 'income')).toBe('income')
    expect(toggleSummaryScope('income', 'income')).toBeNull()
    expect(toggleSummaryScope('income', 'net')).toBe('net')
  })

  it('maps every scope to an existing label key', () => {
    for (const scope of SUMMARY_SCOPES) {
      expect(summaryScopeLabelKey(scope)).toMatch(/^transactions\.summary[A-Z]/)
    }
    expect(summaryScopeLabelKey('expense')).toBe('transactions.summaryExpenses')
  })

  it('reads the figure for a scope, treating a missing invested as zero', () => {
    const summary = { income: 10, expense: 4, net: 6, excluded: 3 }
    expect(summaryScopeValue(summary, 'net')).toBe(6)
    expect(summaryScopeValue(summary, 'invested')).toBe(0)
    expect(summaryScopeValue({ ...summary, invested: 2 }, 'invested')).toBe(2)
  })
})
