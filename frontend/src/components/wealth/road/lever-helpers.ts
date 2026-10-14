import type { WealthLeverKey, WealthLeverResult, WealthLevers } from '@/types/wealth'

type Translate = (key: string, options?: Record<string, unknown>) => string
type FormatMoney = (value: number) => string

/** Fixed monthly amounts baked into the lever's own key (plan P5's lever
 *  list) — unlike `idle_cash` and `cut_top`, whose dollar figures come from
 *  the current wealth state (`WealthLevers`), these never change. */
const FIXED_MONTHLY_AMOUNT: Partial<Record<WealthLeverKey, number>> = {
  plus_500: 500,
  plus_1000: 1000,
  plus_2000: 2000,
}

/** A lever is "helpful" when it reaches the goal sooner than the base path
 *  and a date is actually known. Levers that are unreachable or don't move
 *  the date sort to the bottom (R1 S1 rule). */
function isHelpful(lever: WealthLeverResult): boolean {
  return lever.date != null && lever.months_to_target != null && lever.shift_months != null && lever.shift_months < 0
}

/** Sorts by most months saved first; unreachable or no-shift levers last. */
export function sortLevers(levers: WealthLeverResult[]): WealthLeverResult[] {
  return [...levers].sort((a, b) => {
    const aHelpful = isHelpful(a)
    const bHelpful = isHelpful(b)
    if (aHelpful !== bHelpful) return aHelpful ? -1 : 1
    return (a.shift_months ?? 0) - (b.shift_months ?? 0)
  })
}

export interface LeverCopy {
  key: WealthLeverKey
  /** "What it is" — e.g. "Invest your idle cash ($26,000.00)". */
  whatItIs: string
  /** The date shift — e.g. "−4 months → Q2 2033". */
  shift: string
}

/**
 * The "what it is" line's dollar figure. `idle_cash` and `cut_top` read
 * from the user's own data (`WealthLevers`), so their amount is formatted
 * with `maskedMoney` and disappears in privacy mode. `plus_500`/`1000`/
 * `2000` are fixed hypothetical increments baked into the lever key, not
 * anyone's data, so they're formatted with the plain `money` and stay
 * visible in privacy mode — otherwise all three cards read "+••••• a
 * month" and can't be told apart (R1 gate finding #2).
 */
function whatItIs(
  lever: WealthLeverResult,
  levers: WealthLevers | undefined,
  t: Translate,
  money: FormatMoney,
  maskedMoney: FormatMoney,
): string | null {
  switch (lever.key) {
    case 'idle_cash': {
      const amount = levers?.idle_cash ?? lever.amount ?? null
      return amount == null ? null : t('wealthRoad.levers.idleCash', { amount: maskedMoney(amount) })
    }
    case 'plus_500':
    case 'plus_1000':
    case 'plus_2000': {
      const amount = FIXED_MONTHLY_AMOUNT[lever.key] ?? lever.amount ?? null
      return amount == null ? null : t('wealthRoad.levers.plusMonthly', { amount: money(amount) })
    }
    case 'cut_top': {
      const category = levers?.top_controllable?.category
      const amount = levers?.top_controllable?.band_cut ?? lever.amount ?? null
      if (!category || amount == null) return null
      return t('wealthRoad.levers.cutTop', { category, amount: maskedMoney(amount) })
    }
    case 'return_plus_1':
      return t('wealthRoad.levers.returnHigher')
    case 'return_minus_1':
      return t('wealthRoad.levers.returnLower')
    default:
      return null
  }
}

/** "−4" / "+6" — the sign carries the meaning (sooner vs. later), so it's
 *  built in code rather than left to a translation's word order. */
export function formatSignedMonths(shiftMonths: number): string {
  const sign = shiftMonths < 0 ? '−' : '+'
  return `${sign}${Math.abs(shiftMonths)}`
}

/** The second line — the date shift, e.g. "−4 months → Q2 2033". Levers with
 *  no known shift or date are described as not reachable instead. */
function shiftText(lever: WealthLeverResult, t: Translate): string {
  if (lever.shift_months == null || lever.label == null) {
    return t('wealthRoad.levers.notReachable')
  }
  if (lever.shift_months === 0) {
    return t('wealthRoad.levers.noShift', { label: lever.label })
  }
  return t('wealthRoad.levers.shift', { months: formatSignedMonths(lever.shift_months), label: lever.label })
}

/** Builds the lever card copy, or `null` when the lever can't be described
 *  from what's loaded (e.g. `cut_top` before the levers endpoint resolves). */
export function describeLever(
  lever: WealthLeverResult,
  levers: WealthLevers | undefined,
  t: Translate,
  money: FormatMoney,
  maskedMoney: FormatMoney,
): LeverCopy | null {
  const line = whatItIs(lever, levers, t, money, maskedMoney)
  if (line == null) return null
  return { key: lever.key, whatItIs: line, shift: shiftText(lever, t) }
}
