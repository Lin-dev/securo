/**
 * Agent proposals: the data shape the MCP `propose_*` tools return, the
 * function that turns an accepted proposal into a real Securo write, and a
 * tiny shared "applied" store so every card (and the Apply-all bar) agrees
 * on what has already been applied — across components and reloads.
 */
import type { QueryClient } from '@tanstack/react-query'
import {
  budgets,
  categories,
  goals,
  recurring,
  rules,
  transactions,
} from '@/lib/api'

export type ProposalKind =
  | 'categorize'
  | 'create_category'
  | 'create_budget'
  | 'create_payee_rule'
  | 'create_transaction'
  | 'create_recurring_transaction'
  | 'update_recurring_transaction'
  | 'cancel_recurring_transaction'
  | 'create_goal'

export interface ProposalData {
  kind?: ProposalKind
  proposed?: Record<string, unknown>
  target?: Record<string, unknown>
  changes?: Record<string, unknown>
  affected?: { id: string; description?: string; amount?: number; currency?: string }[]
  affected_count?: number
  target_category?: { id: string; name: string }
  name_collision?: { id: string; name: string }
  mode?: 'deactivate' | 'delete'
  apply_endpoint?: string
  error?: string
}

const PROPOSAL_KINDS: readonly string[] = [
  'categorize', 'create_category', 'create_budget', 'create_payee_rule',
  'create_transaction', 'create_recurring_transaction',
  'update_recurring_transaction', 'cancel_recurring_transaction',
  'create_goal',
]

/** Heuristic: a tool result is a proposal if its data has a known kind. */
export function isProposalData(data: unknown): data is ProposalData {
  if (!data || typeof data !== 'object') return false
  const k = (data as { kind?: unknown }).kind
  return typeof k === 'string' && PROPOSAL_KINDS.includes(k)
}

/** Treat the data as a proposal even when only `error` is present, since
 * a proposal that failed validation should still render a small error card
 * instead of a generic tool-debug chip. */
export function isProposalToolName(name: string): boolean {
  return name.includes('propose_')
}

/** A proposal the user can still act on (has a kind, no upstream error). */
export function isActionableProposal(data: ProposalData): boolean {
  return !data.error && !!data.kind
}

// ---------------------------------------------------------------- applied store

const APPLIED_KEY = 'securo:agent-proposal-applied'

type AppliedRecord = { ts: number; ref?: string }

const listeners = new Set<() => void>()
// Bumped on every change so components can subscribe to "anything applied"
// without diffing the whole map.
let version = 0

function loadApplied(): Record<string, AppliedRecord> {
  try {
    return JSON.parse(localStorage.getItem(APPLIED_KEY) || '{}')
  } catch {
    return {}
  }
}

export function getAppliedRecord(toolCallId: string): AppliedRecord | null {
  return loadApplied()[toolCallId] ?? null
}

export function isProposalApplied(toolCallId: string): boolean {
  return getAppliedRecord(toolCallId) !== null
}

/** Persist the "applied" marker for a card and notify every subscriber. */
export function markProposalApplied(toolCallId: string, ref?: string): void {
  const all = loadApplied()
  all[toolCallId] = { ts: Date.now(), ref }
  try {
    localStorage.setItem(APPLIED_KEY, JSON.stringify(all))
  } catch {
    // storage unavailable — the in-memory notification still updates the UI
  }
  version += 1
  listeners.forEach((cb) => cb())
}

export function subscribeApplied(cb: () => void): () => void {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}

export function appliedVersion(): number {
  return version
}

// ---------------------------------------------------------------- applying

/** Query keys a proposal may have changed; invalidated after every apply. */
export const PROPOSAL_QUERY_KEYS = [
  'transactions',
  'accounts',
  'categories',
  'category-groups',
  'recurring-transactions',
  'budgets',
  'rules',
  'goals',
  'dashboard',
] as const

export function invalidateProposalQueries(qc: QueryClient): void {
  PROPOSAL_QUERY_KEYS.forEach((key) => qc.invalidateQueries({ queryKey: [key] }))
}

/** Best-effort human-readable detail from an axios-style error. */
export function applyErrorDetail(err: unknown): string | undefined {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string' && detail) return detail
  if (err instanceof Error && err.message) return err.message
  return undefined
}

// Each kind maps to one Securo endpoint already exposed via lib/api.ts.
// Returns a string ref (id of the new entity) when available — used as a
// breadcrumb in the localStorage record so a future "view created entity"
// affordance can deep-link to it.
export async function applyProposal(data: ProposalData): Promise<string | void> {
  const p = (data.proposed || {}) as Record<string, unknown>
  switch (data.kind) {
    case 'categorize': {
      const ids = (data.affected || []).map((a) => a.id)
      const res = await transactions.bulkCategorize(ids, data.target_category!.id)
      return `${res.updated} updated`
    }
    case 'create_category': {
      const c = await categories.create({
        name: String(p.name),
        group_id: (p.group_id as string) || undefined,
        icon: (p.icon as string) || undefined,
        color: (p.color as string) || undefined,
      })
      return c.id
    }
    case 'create_budget': {
      const b = await budgets.create({
        category_id: String(p.category_id),
        amount: Number(p.amount),
        month: String(p.month),
      })
      return b.id
    }
    case 'create_payee_rule': {
      const r = await rules.create({
        name: String(p.name ?? `Rule: ${String(p.match_pattern).slice(0, 60)}`).slice(0, 255),
        conditions_op: 'and',
        conditions: [
          { field: 'description', op: 'contains', value: String(p.match_pattern) },
        ],
        actions: [{ op: 'set_category', value: String(p.category_id) }],
        priority: Number.isFinite(Number(p.priority)) ? Number(p.priority) : 10,
        is_active: true,
      })
      return r.id
    }
    case 'create_transaction': {
      // If the proposal includes group splits, translate the agent's
      // {member_id, share_amount, share_pct} preview into the API's
      // {group_member_id, share_amount, share_pct} schema. The backend
      // service re-runs the math in `equal` mode so passing the per-
      // member amounts back is not required, but we keep them so an
      // exact/percent split round-trips identically to the preview.
      const splitsBlock = p.splits as { share_type?: string; items?: Array<Record<string, unknown>> } | undefined
      const splitsPayload = splitsBlock && Array.isArray(splitsBlock.items) && splitsBlock.items.length > 0
        ? {
            share_type: String(splitsBlock.share_type || 'equal'),
            splits: splitsBlock.items.map((it) => ({
              group_member_id: String(it.member_id),
              ...(it.share_amount != null ? { share_amount: Number(it.share_amount) } : {}),
              ...(it.share_pct != null ? { share_pct: Number(it.share_pct) } : {}),
            })),
          }
        : undefined
      const t = await transactions.create({
        description: String(p.description),
        amount: Number(p.amount),
        currency: (p.currency as string) || undefined,
        type: (p.type as string) || undefined,
        date: (p.date as string) || undefined,
        account_id: (p.account_id as string) || undefined,
        category_id: (p.category_id as string) || undefined,
        notes: (p.notes as string) || undefined,
        ...(splitsPayload ? { splits: splitsPayload } : {}),
      } as Parameters<typeof transactions.create>[0])
      return t.id
    }
    case 'create_recurring_transaction': {
      const rt = await recurring.create({
        description: String(p.description),
        amount: Number(p.amount),
        currency: (p.currency as string) || undefined,
        type: (p.type as string) || undefined,
        frequency: (p.frequency as string) || undefined,
        day_of_month: (p.day_of_month as number) ?? undefined,
        start_date: (p.start_date as string) || undefined,
        end_date: (p.end_date as string) || undefined,
        account_id: (p.account_id as string) || undefined,
        category_id: (p.category_id as string) || undefined,
      } as Parameters<typeof recurring.create>[0])
      return rt.id
    }
    case 'update_recurring_transaction': {
      const id = String((data.target as Record<string, unknown>).id)
      await recurring.update(id, (data.changes || {}) as Parameters<typeof recurring.update>[1])
      return id
    }
    case 'cancel_recurring_transaction': {
      const id = String((data.target as Record<string, unknown>).id)
      if (data.mode === 'delete') {
        await recurring.delete(id)
      } else {
        await recurring.update(id, { is_active: false } as Parameters<typeof recurring.update>[1])
      }
      return id
    }
    case 'create_goal': {
      const g = await goals.create({
        name: String(p.name),
        target_amount: Number(p.target_amount),
        currency: (p.currency as string) || undefined,
        deadline: (p.deadline as string) || undefined,
        initial_amount: (p.initial_amount as number) ?? undefined,
        icon: (p.icon as string) || undefined,
        color: (p.color as string) || undefined,
      } as Parameters<typeof goals.create>[0])
      return g.id
    }
  }
}
