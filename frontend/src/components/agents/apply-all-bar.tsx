import { useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { AlertCircle, Check, CheckCheck, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import {
  applyErrorDetail,
  applyProposal,
  invalidateProposalQueries,
  isActionableProposal,
  isProposalApplied,
  markProposalApplied,
  type ProposalData,
} from '@/lib/agent-proposals'
import { useAppliedVersion } from '@/hooks/use-proposal-applied'

export interface ApplyAllItem {
  toolCallId: string
  data: ProposalData
}

interface Props {
  items: ApplyAllItem[]
}

interface Failure {
  toolCallId: string
  label: string
  detail?: string
}

/**
 * One bar per assistant message that carries several proposal cards. Applies
 * every still-pending card in order, marking each one through the shared
 * applied store so the cards flip to their "applied" state as it goes. A
 * failure is recorded and the run continues with the next card.
 */
export function ApplyAllBar({ items }: Props) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  // Re-render whenever any card is applied (by us or by its own button).
  const version = useAppliedVersion()
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null)
  const [failures, setFailures] = useState<Failure[]>([])
  const [ran, setRan] = useState(false)

  const pending = useMemo(
    () => items.filter((it) => isActionableProposal(it.data) && !isProposalApplied(it.toolCallId)),
    // `version` is the store's change counter; it is the dependency that matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [items, version],
  )

  // Only worth showing when there is more than one card to click through,
  // or to report the outcome of a run that just happened.
  if (pending.length < 2 && !ran) return null

  const applyAll = async () => {
    if (running || pending.length === 0) return
    const batch = pending
    setRunning(true)
    setRan(true)
    setFailures([])
    setProgress({ done: 0, total: batch.length })
    const failed: Failure[] = []
    for (let i = 0; i < batch.length; i++) {
      const it = batch[i]
      try {
        const ref = await applyProposal(it.data)
        markProposalApplied(it.toolCallId, typeof ref === 'string' ? ref : undefined)
      } catch (err) {
        failed.push({ toolCallId: it.toolCallId, label: proposalLabel(it.data, t), detail: applyErrorDetail(err) })
      }
      setProgress({ done: i + 1, total: batch.length })
    }
    setFailures(failed)
    setRunning(false)
    invalidateProposalQueries(qc)
  }

  const total = progress?.total ?? pending.length
  const finished = ran && !running

  return (
    <div
      className={cn(
        'rounded-md border text-sm px-3 py-2 flex flex-col gap-1.5',
        finished && failures.length === 0
          ? 'border-emerald-300/40 bg-emerald-50/30 dark:bg-emerald-950/15'
          : 'border-amber-300/50 bg-amber-50/40 dark:bg-amber-950/15',
      )}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="text-[13px] text-muted-foreground flex items-center gap-2 min-w-0">
          {running ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin shrink-0" />
              <span>{t('agents.proposal.applyAllProgress', { done: progress?.done ?? 0, total })}</span>
            </>
          ) : finished ? (
            failures.length === 0 ? (
              <>
                <Check className="h-3.5 w-3.5 text-emerald-600 shrink-0" />
                <span>{t('agents.proposal.applyAllDone', { count: total })}</span>
              </>
            ) : (
              <>
                <AlertCircle className="h-3.5 w-3.5 text-rose-600 shrink-0" />
                <span>{t('agents.proposal.applyAllFailed', { failed: failures.length, total })}</span>
              </>
            )
          ) : (
            <span>{t('agents.proposal.label')}</span>
          )}
        </div>
        <Button
          size="sm"
          onClick={applyAll}
          disabled={running || pending.length === 0}
          className="shrink-0"
        >
          {running ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />
          ) : (
            <CheckCheck className="h-3.5 w-3.5 mr-1.5" />
          )}
          {t('agents.proposal.applyAll', { count: pending.length })}
        </Button>
      </div>
      {failures.length > 0 && (
        <ul className="text-[12px] text-rose-700 dark:text-rose-200 space-y-0.5">
          {failures.map((f) => (
            <li key={f.toolCallId} className="flex items-start gap-1.5">
              <AlertCircle className="h-3 w-3 mt-0.5 shrink-0" />
              <span className="break-words">
                {f.label}
                {f.detail ? ` — ${f.detail}` : ''}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function proposalLabel(data: ProposalData, t: (key: string) => string): string {
  const p = (data.proposed || {}) as Record<string, unknown>
  const kind = data.kind ? t(`agents.proposal.kind.${data.kind}`) : ''
  const hint = String(p.match_pattern ?? p.name ?? p.description ?? data.target_category?.name ?? '')
  return hint ? `${kind}: ${hint}` : kind
}
