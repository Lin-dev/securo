import { useSyncExternalStore } from 'react'
import { appliedVersion, getAppliedRecord, subscribeApplied } from '@/lib/agent-proposals'

/** Timestamp at which this proposal card was applied, or null. Re-renders
 *  whenever any card is marked applied (by its own button or by Apply all). */
export function useProposalApplied(toolCallId: string): number | null {
  return useSyncExternalStore(
    subscribeApplied,
    () => getAppliedRecord(toolCallId)?.ts ?? null,
    () => null,
  )
}

/** Monotonic counter bumped on every apply; subscribe to recompute derived
 *  state (e.g. "how many of these cards are still pending"). */
export function useAppliedVersion(): number {
  return useSyncExternalStore(subscribeApplied, appliedVersion, () => 0)
}
