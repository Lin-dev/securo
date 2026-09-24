import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { AlertCircle, Check, ChevronDown, ChevronRight, Loader2, Sparkles } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import {
  applyErrorDetail,
  applyProposal,
  invalidateProposalQueries,
  markProposalApplied,
  type ProposalData,
  type ProposalKind,
} from '@/lib/agent-proposals'
import { useProposalApplied } from '@/hooks/use-proposal-applied'

interface Props {
  toolCallId: string
  data: ProposalData
}

export function ProposalCard({ toolCallId, data }: Props) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  // Shared store: flips to applied whether this card's button or the
  // message-level "Apply all" did the work, and survives reloads.
  const appliedAt = useProposalApplied(toolCallId)
  const [showJson, setShowJson] = useState(false)

  const apply = useMutation({
    mutationFn: () => applyProposal(data),
    onSuccess: (ref) => {
      markProposalApplied(toolCallId, typeof ref === 'string' ? ref : undefined)
      toast.success(t('agents.proposal.applied'))
      // Invalidate the broad surface that proposals may have changed.
      invalidateProposalQueries(qc)
    },
    onError: (err: unknown) => {
      toast.error(applyErrorDetail(err) || t('agents.proposal.applyFailed'))
    },
  })

  // Backend already short-circuits with {error: "..."} on bad input.
  if (data.error) {
    return (
      <div className="rounded-md border border-rose-300/50 bg-rose-50/40 dark:bg-rose-950/15 px-3 py-2 text-sm flex items-center gap-2 text-rose-700 dark:text-rose-200">
        <AlertCircle className="h-4 w-4 shrink-0" />
        <span className="truncate">{data.error}</span>
      </div>
    )
  }

  if (!data.kind) return null

  const kind = data.kind
  const summary = renderSummary(kind, data, t)
  const isApplied = appliedAt !== null

  return (
    <div
      className={cn(
        'rounded-md border text-sm overflow-hidden',
        isApplied
          ? 'border-emerald-300/40 bg-emerald-50/30 dark:bg-emerald-950/15'
          : 'border-amber-300/50 bg-amber-50/40 dark:bg-amber-950/15',
      )}
    >
      <div className="px-3 py-2.5 flex items-start gap-3">
        <div className="shrink-0 mt-0.5">
          {isApplied ? (
            <Check className="h-4 w-4 text-emerald-600" />
          ) : (
            <Sparkles className="h-4 w-4 text-amber-600" />
          )}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-semibold">{kindTitle(kind, t)}</span>
            <span
              className={cn(
                'text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded',
                isApplied
                  ? 'bg-emerald-200/70 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200'
                  : 'bg-amber-200/70 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200',
              )}
            >
              {isApplied ? t('agents.proposal.applied') : t('agents.proposal.label')}
            </span>
          </div>
          <div className="mt-1 text-muted-foreground text-[13px] leading-snug">{summary}</div>
          <SplitPreview proposed={data.proposed} />
        </div>
        <div className="shrink-0 flex items-center gap-1.5">
          {isApplied ? (
            <span className="text-xs text-emerald-700 dark:text-emerald-300 inline-flex items-center gap-1">
              <Check className="h-3.5 w-3.5" />
              {t('agents.proposal.applied')}
            </span>
          ) : (
            <Button size="sm" onClick={() => apply.mutate()} disabled={apply.isPending}>
              {apply.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />
              ) : (
                <Check className="h-3.5 w-3.5 mr-1.5" />
              )}
              {t('agents.proposal.apply')}
            </Button>
          )}
        </div>
      </div>
      <button
        type="button"
        onClick={() => setShowJson((v) => !v)}
        className="w-full text-[11px] uppercase tracking-wider text-muted-foreground hover:text-foreground border-t px-3 py-1.5 flex items-center gap-1.5"
      >
        {showJson ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        {t('agents.proposal.showRaw')}
      </button>
      {showJson && (
        <pre className="border-t px-3 py-2 text-[11px] leading-snug font-mono bg-background/60 overflow-x-auto">
          {safeStringify(data)}
        </pre>
      )}
    </div>
  )
}

function kindTitle(kind: ProposalKind, t: ReturnType<typeof useTranslation>['t']): string {
  return t(`agents.proposal.kind.${kind}`)
}

function renderSummary(kind: ProposalKind, d: ProposalData, t: ReturnType<typeof useTranslation>['t']): string {
  const p = (d.proposed || {}) as Record<string, unknown>
  const tgt = (d.target || {}) as Record<string, unknown>
  switch (kind) {
    case 'categorize':
      return t('agents.proposal.summary.categorize', {
        count: d.affected_count ?? d.affected?.length ?? 0,
        category: d.target_category?.name ?? '?',
      })
    case 'create_category':
      return t('agents.proposal.summary.createCategory', {
        name: String(p.name ?? '?'),
      }) + (d.name_collision ? ` — ${t('agents.proposal.collision', { name: d.name_collision.name })}` : '')
    case 'create_budget':
      return t('agents.proposal.summary.createBudget', {
        category: String((p.category_name as string) ?? p.category_id ?? '?'),
        amount: fmt(p.amount, p.currency),
        month: String(p.month ?? '?'),
      })
    case 'create_payee_rule':
      return t('agents.proposal.summary.createPayeeRule', {
        pattern: String(p.match_pattern ?? '?'),
        category: String((p.category_name as string) ?? p.category_id ?? '?'),
      })
    case 'create_transaction':
      return t('agents.proposal.summary.createTransaction', {
        description: String(p.description ?? '?'),
        amount: fmt(p.amount, p.currency),
        type: String(p.type ?? '?'),
        date: String(p.date ?? ''),
        account: String((p.account_name as string) ?? '?'),
      })
    case 'create_recurring_transaction':
      return t('agents.proposal.summary.createRecurring', {
        description: String(p.description ?? '?'),
        amount: fmt(p.amount, p.currency),
        frequency: String(p.frequency ?? '?'),
        day: p.day_of_month ? `${p.day_of_month}` : '?',
        account: String((p.account_name as string) ?? '?'),
      })
    case 'update_recurring_transaction': {
      const changes = (d.changes || {}) as Record<string, unknown>
      const lines = Object.entries(changes).map(([k, v]) => {
        const before = tgt[k]
        return `${k}: ${String(before ?? '∅')} → ${String(v ?? '∅')}`
      })
      return t('agents.proposal.summary.updateRecurring', {
        description: String(tgt.description ?? '?'),
        changes: lines.join(', ') || '—',
      })
    }
    case 'cancel_recurring_transaction':
      return t(d.mode === 'delete' ? 'agents.proposal.summary.deleteRecurring' : 'agents.proposal.summary.deactivateRecurring', {
        description: String(tgt.description ?? '?'),
      })
    case 'create_goal':
      return t('agents.proposal.summary.createGoal', {
        name: String(p.name ?? '?'),
        target: fmt(p.target_amount, p.currency),
        deadline: p.deadline ? String(p.deadline) : '—',
      })
    default:
      return ''
  }
}

function fmt(amount: unknown, currency: unknown): string {
  if (amount == null) return '?'
  const n = typeof amount === 'number' ? amount : Number(amount) || 0
  const c = typeof currency === 'string' ? currency : 'BRL'
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency: c }).format(n)
  } catch {
    return `${c} ${n.toFixed(2)}`
  }
}

function safeStringify(o: unknown): string {
  try {
    return JSON.stringify(o, null, 2)
  } catch {
    return String(o)
  }
}

function SplitPreview({ proposed }: { proposed?: Record<string, unknown> }) {
  const p = proposed || {}
  const splits = p.splits as { share_type?: string; items?: Array<Record<string, unknown>> } | undefined
  if (!splits || !Array.isArray(splits.items) || splits.items.length === 0) return null
  const groupName = (p.group_name as string) || ''
  const currency = typeof p.currency === 'string' ? p.currency : 'BRL'
  const shareType = splits.share_type || 'equal'
  return (
    <div className="mt-1.5 text-[12px] border rounded px-2 py-1.5 bg-background/40">
      <div className="text-muted-foreground mb-1">
        {groupName ? `${groupName} · ` : ''}{shareType}
      </div>
      <ul className="space-y-0.5">
        {splits.items.map((s, i) => (
          <li key={i} className="flex justify-between gap-3">
            <span>{String(s.member_name ?? s.member_id ?? '?')}</span>
            <span className="tabular-nums">
              {fmt(s.share_amount, currency)}
              {s.share_pct != null ? ` (${Number(s.share_pct)}%)` : ''}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
