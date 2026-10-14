import { describe, expect, it } from 'vitest'

import { WEALTH_TABS, wealthTabFromParam } from './wealth-tabs'

describe('wealthTabFromParam', () => {
  it('opens the milestone tab by default', () => {
    expect(wealthTabFromParam(null)).toBe('road')
    expect(WEALTH_TABS[0]).toBe('road')
  })

  it('keeps a shipped tab from the URL', () => {
    expect(wealthTabFromParam('networth')).toBe('networth')
    expect(wealthTabFromParam('settings')).toBe('settings')
  })

  it('falls back for an unknown or not-yet-shipped tab', () => {
    expect(wealthTabFromParam('allocation')).toBe('road')
    expect(wealthTabFromParam('constructor')).toBe('road')
  })
})
