/**
 * "Categories creeping up" (plan R2/S6; critique S13 -- small multiples were
 * cut, this list was kept because it answers the same question more
 * cheaply). A port of the backend's pure `category_spikes`
 * (`app/services/wealth/spending.py`), so the frontend can show the same
 * answer today's loaded report data gives, without a round trip: a
 * category's LATEST COMPLETE month counts as "creeping up" when it's above
 * 1.5x its usual level (`SPIKE_RATIO`) and at least $200 over it
 * (`SPIKE_MIN_EXCESS`). The usual level is the mean of up to the 6 complete
 * months before it, among months present in the data; fewer than 3 such
 * months (or fewer than 3 with a nonzero value) means "not enough history"
 * -- `null`, never a guess. "Uncategorized"/"Other" never count (matches
 * `NON_CATEGORY_RE`): a catch-all bucket swelling isn't a lever.
 */

const SPIKE_RATIO = 1.5
const SPIKE_MIN_EXCESS = 200

// Mirrors `app/services/wealth/patterns.py`'s `NON_CATEGORY_RE`.
const NON_CATEGORY_RE = /^\s*(uncategori[sz]ed|other\b.*)$/i

/** A month label ("YYYY-MM") -> category key -> that category's total that month. */
export type MonthCategoryTotals = Record<string, number>
export type MonthCatsMap = Record<string, MonthCategoryTotals>

export interface CategorySpike {
  category: string
  amount: number
  baseline: number
  excess: number
}

function round2(n: number): number {
  return Math.round(n * 100) / 100
}

/** The month label `count` calendar months before `month` ("YYYY-MM" in, "YYYY-MM" out). */
function monthBefore(month: string, count: number): string {
  const [y, m] = month.split('-').map(Number)
  let year = y
  let mon = m - count
  while (mon <= 0) {
    mon += 12
    year -= 1
  }
  return `${year}-${String(mon).padStart(2, '0')}`
}

/**
 * Direct port of `category_spikes(month_cats, month)`. Generic over
 * whatever month-keyed map the caller builds, so the backend's exact test
 * cases carry over unchanged.
 */
export function categorySpikes(monthCats: MonthCatsMap, month: string): CategorySpike[] | null {
  const baseMonths: string[] = []
  for (let back = 1; back <= 6; back++) {
    const bm = monthBefore(month, back)
    if (bm in monthCats) baseMonths.push(bm)
  }
  if (baseMonths.length < 3 || !(month in monthCats)) return null

  const out: CategorySpike[] = []
  const monthTotals = monthCats[month]
  for (const [cat, value] of Object.entries(monthTotals)) {
    if (NON_CATEGORY_RE.test(cat ?? '')) continue
    const series = baseMonths.map((bm) => monthCats[bm]?.[cat] ?? 0)
    if (series.filter((x) => x > 0).length < 3) continue
    const baseline = series.reduce((a, b) => a + b, 0) / series.length
    const excess = value - baseline
    if (value > SPIKE_RATIO * baseline && excess >= SPIKE_MIN_EXCESS) {
      out.push({ category: cat, amount: round2(value), baseline: round2(baseline), excess: round2(excess) })
    }
  }
  return out.sort((a, b) => b.excess - a.excess)
}

// --- Frontend adapter ------------------------------------------------------

export interface CategoryCreepSeriesPoint {
  date: string
  value: number
}

export interface CategoryCreepCategory {
  key: string
  label: string
  color: string
  /** Monthly data points; `date` is expected as `"YYYY-MM"` or
   * `"YYYY-MM-DD"` (only the first 7 characters are read). */
  series: readonly CategoryCreepSeriesPoint[]
}

export interface CategoryCreepItem {
  key: string
  label: string
  color: string
  /** The latest complete month this spike was found in ("YYYY-MM"). */
  month: string
  amount: number
  baseline: number
  excess: number
}

/**
 * Builds the month map from the income/expenses report's own
 * `category_trend` (expense categories only -- pass the already-filtered
 * list), finds the latest month strictly before `currentMonth` ("YYYY-MM",
 * the caller's own `today`'s month in the workspace timezone -- never the
 * browser's), and runs `categorySpikes` against it.
 *
 * Returns `null` when there's no complete month to check at all (an empty
 * series, or a range that only covers the current, still-open month) --
 * "not enough history", same as the backend returning `null`.
 */
export function categoriesCreepingUp(
  categories: readonly CategoryCreepCategory[],
  currentMonth: string,
): CategoryCreepItem[] | null {
  const monthCats: MonthCatsMap = {}
  const labelOf = new Map<string, string>()
  const colorOf = new Map<string, string>()

  for (const cat of categories) {
    labelOf.set(cat.key, cat.label)
    colorOf.set(cat.key, cat.color)
    for (const point of cat.series) {
      const month = point.date.slice(0, 7)
      const bucket = (monthCats[month] ??= {})
      bucket[cat.key] = (bucket[cat.key] ?? 0) + point.value
    }
  }

  const latest = Object.keys(monthCats)
    .filter((m) => m < currentMonth)
    .sort()
    .at(-1)
  if (!latest) return null

  const spikes = categorySpikes(monthCats, latest)
  if (spikes === null) return null

  return spikes.map((s) => ({
    key: s.category,
    label: labelOf.get(s.category) ?? s.category,
    color: colorOf.get(s.category) ?? '#9CA3AF',
    month: latest,
    amount: s.amount,
    baseline: s.baseline,
    excess: s.excess,
  }))
}
