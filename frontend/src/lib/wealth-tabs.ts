/**
 * Tabs of the wealth page (fork). A tab is listed here only once its release
 * ships it: cash arrives in R2, allocation and limits in R3, so the latter are not offered yet,
 * and an unknown or not-yet-shipped `?tab=` falls back to the first tab.
 */
export const WEALTH_TABS = ['road', 'networth', 'savings', 'cash', 'settings'] as const

export type WealthTab = (typeof WEALTH_TABS)[number]

export function wealthTabFromParam(value: string | null): WealthTab {
  return (WEALTH_TABS as readonly string[]).includes(value ?? '') ? (value as WealthTab) : WEALTH_TABS[0]
}
