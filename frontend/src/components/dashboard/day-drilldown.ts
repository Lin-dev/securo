/**
 * Resolves the day a click on the dashboard's balance-evolution chart
 * landed on (plan S3 bug fix).
 *
 * recharts 3.7 removed `activePayload` from the chart's click/mouse state
 * (it only ever existed on the underlying native event, never documented on
 * the state object), which silently broke the day drill-down: the handler
 * read `state.activePayload`, always got `undefined`, and nothing opened.
 *
 * The replacement, `activeTooltipIndex`, is an index into the chart's own
 * `data` array (the same value the chart's `onMouseMove` handler already
 * reads for hover). It arrives as a number, a numeric string, or
 * null/undefined depending on how the click landed, so this resolver
 * normalizes all of those before indexing.
 */
export function resolveClickedDay(
  activeTooltipIndex: number | string | null | undefined,
  data: ReadonlyArray<{ day: number }>,
): number | null {
  if (activeTooltipIndex == null) return null
  const index = typeof activeTooltipIndex === 'number' ? activeTooltipIndex : Number(activeTooltipIndex)
  if (!Number.isInteger(index) || index < 0 || index >= data.length) return null
  return data[index]?.day ?? null
}
