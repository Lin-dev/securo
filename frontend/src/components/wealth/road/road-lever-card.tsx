import type { LeverCopy } from './lever-helpers'

export interface RoadLeverCardProps {
  copy: LeverCopy
}

/**
 * One lever: what it is, and the date shift it buys. The amount inside
 * `copy.whatItIs` is already masked by the caller (the lever's wording
 * should survive privacy mode — only the dollar figure disappears), so
 * this renders both strings verbatim.
 */
export function RoadLeverCard({ copy }: RoadLeverCardProps) {
  return (
    <div className="rounded-lg border border-border/60 bg-card p-4 space-y-1.5">
      <p className="text-sm font-medium text-foreground">{copy.whatItIs}</p>
      <p className="text-sm text-muted-foreground tabular-nums">{copy.shift}</p>
    </div>
  )
}
