import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import {
  sankey as d3Sankey,
  sankeyLinkHorizontal,
  sankeyJustify,
} from 'd3-sankey'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import type { ReportCompositionItem } from '@/types'
import {
  accountIdFromKey,
  buildMoneyMap,
  categoryIdFromKey,
  EXPENSE_COLOR,
  INVEST_COLOR,
  laneIds,
  LEFT_SIDES,
  RIGHT_SIDES,
  SURPLUS_COLOR,
  TRANSFER_COLOR,
  type MoneyMapLink as SankeyLinkDatum,
  type MoneyMapNode as SankeyNodeDatum,
  type MoneyMapSide,
} from '@/lib/money-map-utils'
import { resolveChartClick, type ChartClickResolution } from '@/lib/drilldown'
import { TransactionDrillDown, type DrillDownFilter } from '@/components/transaction-drill-down'

// Colours, lane rules and node/link construction live in money-map-utils so
// they can be unit-tested; this file is the SVG.

const NODE_WIDTH = 16
const NODE_PADDING = 16
// Vertical space a two-line label (name + amount) needs; labels are pushed
// apart to at least this gap so thin adjacent nodes stay readable.
const LABEL_MIN_GAP = 26
const TOP_GUTTER = 30 // header room above the nodes for the centre label

// What the cursor is isolating. A plain node hover lights its own flows; the
// segments of the centre bar light *all* expenses, investments, transfers or
// the surplus at once — the "show me everything I spent" gesture the single
// bar couldn't offer.
type Hover =
  | { kind: 'node'; index: number }
  | { kind: 'expenses' }
  | { kind: 'investments' }
  | { kind: 'transfers' }
  | { kind: 'surplus' }

function formatMoney(value: number, currency: string, locale: string, compact: boolean) {
  return new Intl.NumberFormat(locale, {
    style: 'currency',
    currency,
    notation: compact ? 'compact' : 'standard',
    maximumFractionDigits: compact ? 1 : 0,
  }).format(value)
}

interface CashflowSankeyProps {
  composition: ReportCompositionItem[]
  currency: string
  locale: string
  /** The report's own [window_from, window_to] (plan B5c's `meta`), so a
   * click lands on exactly the rows the chart drew from -- not a window
   * re-derived from whatever range control is showing. */
  window: { from: string; to: string }
}

// Which lane a side's rows fall under for the P1 filter: income/transfer_in
// rows are always credits, expense/investment/transfer_out rows are always
// debits (see report_service.py's Money Map lanes) -- `contribution` has no
// single type of its own (see `handleNodeActivate`).
const CREDIT_SIDES: ReadonlySet<MoneyMapSide> = new Set(['income', 'transfer_in'])
const DEBIT_SIDES: ReadonlySet<MoneyMapSide> = new Set(['expense', 'investment', 'transfer_out'])
function typeForSide(side: MoneyMapSide): 'credit' | 'debit' | undefined {
  if (CREDIT_SIDES.has(side)) return 'credit'
  if (DEBIT_SIDES.has(side)) return 'debit'
  return undefined
}

type Lane = 'income' | 'expenses' | 'investments' | 'transfers'

export function CashflowSankey({ composition, currency, locale, window }: CashflowSankeyProps) {
  const { t } = useTranslation()
  const { privacyMode, MASK } = usePrivacyMode()
  const navigate = useNavigate()
  const containerRef = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)
  const [hover, setHover] = useState<Hover | null>(null)
  // The drill-down this chart owns (plan S5): every node/lane click opens it
  // here, scoped to `window`, rather than threading click state up into the
  // Reports page (left to S6, which wires its OWN charts' clicks).
  const [drillFilter, setDrillFilter] = useState<DrillDownFilter | null>(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const observer = new ResizeObserver((entries) => {
      setWidth(entries[0].contentRect.width)
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  const { nodes: rawNodes, links: rawLinks, hasData, totals } = useMemo(
    () => buildMoneyMap(composition, {
      other: t('reports.other'),
      uncategorized: t('reports.uncategorized'),
      deficit: t('reports.deficit'),
      surplus: t('reports.surplus'),
      center: t('reports.cashFlowNode'),
      directContributions: t('reports.directContributions'),
    }),
    [composition, t],
  )

  // Tall enough that the busier side's nodes don't crowd; grows with node count.
  const maxSide = useMemo(() => {
    const inflowCount = rawNodes.filter((n) => LEFT_SIDES.has(n.side)).length
    const outflowCount = rawNodes.filter((n) => RIGHT_SIDES.has(n.side)).length
    return Math.max(inflowCount, outflowCount, 1)
  }, [rawNodes])
  const height = Math.min(780, Math.max(380, maxSide * 60))

  const layout = useMemo(() => {
    if (!hasData || width === 0) return null
    const generator = d3Sankey<SankeyNodeDatum, SankeyLinkDatum>()
      .nodeWidth(NODE_WIDTH)
      .nodePadding(NODE_PADDING)
      .nodeAlign(sankeyJustify)
      .extent([
        [8, TOP_GUTTER],
        [width - 8, height - 14],
      ])
    // d3-sankey mutates its input — clone so re-renders stay pure.
    return generator({
      nodes: rawNodes.map((n) => ({ ...n })),
      links: rawLinks.map((l) => ({ ...l })),
    })
  }, [rawNodes, rawLinks, width, height, hasData])

  // Always-on labels: place each node's label and push overlapping neighbours
  // apart per column so even a 1px-tall node keeps a readable name + amount.
  const labelY = useMemo(() => {
    const pos = new Map<number, number>()
    if (!layout) return pos
    for (const left of [true, false]) {
      const column = layout.nodes
        .filter((n) => n.id !== 'center')
        .filter((n) => ((n.x0 ?? 0) < width / 2) === left)
        .sort((a, b) => ((a.y0! + a.y1!) / 2) - ((b.y0! + b.y1!) / 2))
      let prev = -Infinity
      for (const n of column) {
        let y = (n.y0! + n.y1!) / 2
        if (y < prev + LABEL_MIN_GAP) y = prev + LABEL_MIN_GAP
        pos.set((n as SankeyNodeDatum & { index: number }).index, y)
        prev = y
      }
    }
    return pos
  }, [layout, width])

  // Resolve the current hover into the exact set of links/nodes to keep lit.
  const { activeLinks, activeNodes } = useMemo(() => {
    const links = new Set<number>()
    const nodes = new Set<number>()
    if (layout && hover) {
      layout.links.forEach((lk, i) => {
        const s = lk.source as SankeyNodeDatum & { index: number }
        const tg = lk.target as SankeyNodeDatum & { index: number }
        let on = false
        if (hover.kind === 'node') on = s.index === hover.index || tg.index === hover.index
        else if (hover.kind === 'expenses') on = s.id === 'center' && tg.side === 'expense' && tg.id !== 'surplus'
        else if (hover.kind === 'investments') on = s.id === 'center' && tg.side === 'investment'
        else if (hover.kind === 'transfers') on = s.id === 'center' && tg.side === 'transfer_out'
        else if (hover.kind === 'surplus') on = tg.id === 'surplus'
        if (on) {
          links.add(i)
          nodes.add(s.index)
          nodes.add(tg.index)
        }
      })
    }
    return { activeLinks: links, activeNodes: nodes }
  }, [layout, hover])

  if (!hasData) {
    return (
      <p className="text-muted-foreground text-sm text-center py-16">{t('reports.noData')}</p>
    )
  }

  const linkPath = sankeyLinkHorizontal<SankeyNodeDatum, SankeyLinkDatum>()
  // Percentages are relative to total cash-flow throughput — the centre node's
  // value (d3-sankey sets it to the larger of its in/out sums).
  const total = layout?.nodes.find((n) => n.id === 'center')?.value ?? 0
  // Any income-side node sits in the income column (every income row links
  // straight into `center`, one hop, same as every other left-side node --
  // they all share a column under `sankeyJustify`); its x0 anchors the
  // income lane's own header below.
  const incomeNode = layout?.nodes.find((n) => n.side === 'income')
  const fmtAmount = (v: number) =>
    privacyMode ? MASK : formatMoney(v, currency, locale, true)
  const nodeAriaLabel = (label: string, value: number) =>
    t('reports.sankeyNodeAriaLabel', { label, amount: fmtAmount(value) })

  // Turn a resolved chart click into either a Transactions navigation or a
  // local drill-down (plan S5). `exact` on a `'lane'` result only affects
  // bookkeeping upstream (`resolveChartClick`'s own doc comment) -- here it
  // just becomes the filter the shared slide-over already knows how to show.
  const openResolution = (result: ChartClickResolution, title: string) => {
    if (result.kind === 'url') {
      navigate(result.url)
      return
    }
    setDrillFilter({
      title,
      category_id: result.kind === 'single' ? result.categoryId : undefined,
      account_id: result.kind === 'single' ? result.accountId : undefined,
      type: result.type,
      wealthPnlOnly: result.wealthPnlOnly,
      from: result.from,
      to: result.to,
    })
  }

  // A category/account node, or "Other" (its `foldedIds`). `contributions`
  // is synthetic -- the backend folds every direct-contribution account into
  // one literal `direct_contributions` token (`report_service.py`), so no
  // account id survives to drill into; its best-effort view is the window,
  // typed as a credit, same as the `investments` lane's own fallback below.
  const handleNodeActivate = (node: SankeyNodeDatum) => {
    if (node.id === 'center' || node.id === 'surplus' || node.id === 'deficit') return
    if (node.id === 'contributions') {
      openResolution(
        { kind: 'lane', exact: false, type: 'credit', from: window.from, to: window.to },
        node.name,
      )
      return
    }
    const categoryIds = node.foldedIds
      ? node.foldedIds.flatMap((key) => {
          const id = categoryIdFromKey(key)
          return id ? [id] : []
        })
      : node.categoryId ? [node.categoryId] : []
    const accountIds = node.foldedIds
      ? node.foldedIds.flatMap((key) => {
          const id = accountIdFromKey(key)
          return id ? [id] : []
        })
      : node.accountId ? [node.accountId] : []
    const wealthPnlOnly = node.side === 'income' || node.side === 'expense'
    openResolution(
      resolveChartClick({ categoryIds, accountIds, window, type: typeForSide(node.side), wealthPnlOnly }),
      node.name,
    )
  }

  // A whole lane (plan S5): income/expenses are exact under the P1 filter
  // (every row IS that `type`); investments/transfers have no such shortcut,
  // so they fall back to the lane's own real category ids when there are
  // any, or to the window alone when the lane is entirely account-keyed or
  // uncategorized rows that collapse() happens not to have folded (a rare
  // edge case, documented rather than silently wrong).
  const handleLaneActivate = (lane: Lane) => {
    const label = t(`reports.${lane}`)
    if (lane === 'income') {
      openResolution(
        { kind: 'lane', exact: true, type: 'credit', wealthPnlOnly: true, from: window.from, to: window.to },
        label,
      )
      return
    }
    if (lane === 'expenses') {
      openResolution(
        { kind: 'lane', exact: true, type: 'debit', wealthPnlOnly: true, from: window.from, to: window.to },
        label,
      )
      return
    }
    const groups = lane === 'investments' ? ['investments'] : ['transfers_in', 'transfers_out']
    const { categoryIds, accountIds } = laneIds(composition, groups)
    openResolution(
      resolveChartClick({
        categoryIds, accountIds, window,
        type: lane === 'investments' ? 'debit' : undefined,
      }),
      label,
    )
  }

  // The centre bar's outflow split: spent (red), invested (blue), moved out
  // (violet), kept (green). Links attach top→bottom in node order (expenses,
  // investments, transfers out, surplus), so stacking the bar's colours in
  // that order lines up with where the flows actually leave the hub.
  const outBy = (pred: (n: SankeyNodeDatum) => boolean) => layout
    ? layout.links
        .filter((l) => (l.source as SankeyNodeDatum).id === 'center' && pred(l.target as SankeyNodeDatum))
        .reduce((s, l) => s + l.value, 0)
    : 0
  const expenseOut = outBy((n) => n.side === 'expense' && n.id !== 'surplus')
  const investOut = outBy((n) => n.side === 'investment')
  const transferOut = outBy((n) => n.side === 'transfer_out')
  const surplusOut = outBy((n) => n.id === 'surplus')

  const linkDimmed = (i: number) => hover !== null && !activeLinks.has(i)
  const nodeDimmed = (idx: number) => hover !== null && !activeNodes.has(idx)

  return (
    <div ref={containerRef} className="w-full privacy-sensitive">
      {layout && (
        <svg
          width={width}
          height={height}
          role="img"
          aria-label={t('reports.flowChartAria')}
          onMouseLeave={() => setHover(null)}
        >
          <defs>
            {layout.links.map((link, i) => {
              const src = link.source as SankeyNodeDatum & { x1: number }
              const tgt = link.target as SankeyNodeDatum & { x0: number }
              return (
                <linearGradient
                  key={i}
                  id={`flow-grad-${i}`}
                  gradientUnits="userSpaceOnUse"
                  x1={src.x1}
                  x2={tgt.x0}
                >
                  <stop offset="0%" stopColor={src.color} />
                  <stop offset="100%" stopColor={tgt.color} />
                </linearGradient>
              )
            })}
          </defs>

          {/* Links — gradient from source colour to target colour */}
          <g fill="none">
            {layout.links.map((link, i) => {
              const dimmed = linkDimmed(i)
              const active = hover !== null && activeLinks.has(i)
              const pct = total > 0 ? ((link.value / total) * 100).toFixed(1) : '0'
              return (
                <path
                  key={i}
                  d={linkPath(link) ?? undefined}
                  stroke={`url(#flow-grad-${i})`}
                  strokeOpacity={dimmed ? 0.07 : active ? 0.6 : 0.4}
                  strokeWidth={Math.max(1.5, link.width ?? 1)}
                  style={{ transition: 'stroke-opacity 0.2s ease' }}
                >
                  <title>
                    {(link.target as SankeyNodeDatum).id === 'center'
                      ? (link.source as SankeyNodeDatum).name
                      : (link.target as SankeyNodeDatum).name}
                    : {fmtAmount(link.value)} ({pct}%)
                  </title>
                </path>
              )
            })}
          </g>

          {/* Nodes + always-on labels */}
          <g>
            {layout.nodes.map((node, i) => {
              const x0 = node.x0 ?? 0
              const x1 = node.x1 ?? 0
              const y0 = node.y0 ?? 0
              const y1 = node.y1 ?? 0
              const nodeHeight = Math.max(1, y1 - y0)
              const isCenter = node.id === 'center'
              const dimmed = nodeDimmed(i)
              const nodeWidthPx = Math.max(1, x1 - x0)

              // Centre bar is split into spent (red), invested (blue), moved
              // out (violet) and kept (green) zones, each its own hover target
              // so the cursor can isolate ALL of that flow type at once. Label
              // sits in the top gutter. Segments are stacked in the same order
              // the links leave the hub (expenses, investments, transfers out,
              // surplus).
              if (isCenter) {
                const cx = (x0 + x1) / 2
                const out = expenseOut + investOut + transferOut + surplusOut || 1
                const redH = nodeHeight * (expenseOut / out)
                const blueH = nodeHeight * (investOut / out)
                const violetH = nodeHeight * (transferOut / out)
                // Only expenses/investments/transfers are a lane a click can
                // open (plan S5); surplus is leftover cash, not a set of
                // transactions, so it keeps the hover isolation but stays
                // out of the tab order.
                const segments: Array<{ h: number; fill: string; hover: Hover; title: string; val: number; lane?: Lane }> = [
                  { h: redH, fill: EXPENSE_COLOR, hover: { kind: 'expenses' }, title: t('reports.expenses'), val: expenseOut, lane: 'expenses' },
                  { h: blueH, fill: INVEST_COLOR, hover: { kind: 'investments' }, title: t('reports.investments'), val: investOut, lane: 'investments' },
                  { h: violetH, fill: TRANSFER_COLOR, hover: { kind: 'transfers' }, title: t('reports.transfersOut'), val: transferOut, lane: 'transfers' },
                  { h: Math.max(0, nodeHeight - redH - blueH - violetH), fill: SURPLUS_COLOR, hover: { kind: 'surplus' }, title: t('reports.surplus'), val: surplusOut },
                ]
                let segY = y0
                return (
                  <g key={i} style={{ transition: 'opacity 0.2s ease', opacity: dimmed ? 0.4 : 1 }}>
                    {segments.filter((seg) => seg.val > 0).map((seg, si) => {
                      const yy = segY
                      segY += seg.h
                      return (
                        <rect
                          key={si}
                          x={x0} y={yy} width={nodeWidthPx} height={Math.max(1, seg.h)}
                          fill={seg.fill} rx={4}
                          strokeWidth={seg.lane ? 2 : undefined}
                          role={seg.lane ? 'button' : undefined}
                          tabIndex={seg.lane ? 0 : undefined}
                          aria-label={seg.lane ? nodeAriaLabel(seg.title, seg.val) : undefined}
                          className={seg.lane ? 'cursor-pointer focus:outline-none focus-visible:stroke-foreground' : undefined}
                          onMouseEnter={() => setHover(seg.hover)}
                          onClick={seg.lane ? () => handleLaneActivate(seg.lane!) : undefined}
                          onKeyDown={seg.lane ? (e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault()
                              handleLaneActivate(seg.lane!)
                            }
                          } : undefined}
                        >
                          <title>{seg.title}: {fmtAmount(seg.val)}</title>
                        </rect>
                      )
                    })}
                    <text x={cx} y={10} textAnchor="middle" className="fill-foreground pointer-events-none" style={{ fontSize: 11, fontWeight: 600 }}>
                      {node.name}
                      <tspan x={cx} dy={13} className="fill-muted-foreground" style={{ fontSize: 10, fontFamily: 'var(--font-mono, monospace)', fontWeight: 400 }}>
                        {fmtAmount(node.value ?? 0)}
                      </tspan>
                    </text>
                  </g>
                )
              }

              const onLeft = x0 < width / 2
              const labelX = onLeft ? x1 + 8 : x0 - 8
              const ly = labelY.get(i) ?? (y0 + y1) / 2
              // Surplus/deficit are derived balances, not a set of rows --
              // nothing to drill into, so they skip the tab order entirely.
              const interactive = node.id !== 'surplus' && node.id !== 'deficit'
              const activate = () => handleNodeActivate(node)
              return (
                <g
                  key={i}
                  role={interactive ? 'button' : undefined}
                  tabIndex={interactive ? 0 : undefined}
                  aria-label={interactive ? nodeAriaLabel(node.name, node.value ?? 0) : undefined}
                  className={interactive ? 'cursor-pointer focus:outline-none focus-visible:opacity-80' : undefined}
                  style={{ transition: 'opacity 0.2s ease', opacity: dimmed ? 0.35 : 1 }}
                  onMouseEnter={() => setHover({ kind: 'node', index: i })}
                  onClick={interactive ? activate : undefined}
                  onKeyDown={interactive ? (e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      activate()
                    }
                  } : undefined}
                >
                  <rect x={x0} y={y0} width={Math.max(1, x1 - x0)} height={nodeHeight} fill={node.color} rx={3}>
                    <title>{node.name}: {fmtAmount(node.value ?? 0)}</title>
                  </rect>
                  <text
                    x={labelX}
                    y={ly}
                    textAnchor={onLeft ? 'start' : 'end'}
                    dominantBaseline="middle"
                    className="fill-foreground"
                    style={{ fontSize: 11, fontWeight: 500 }}
                  >
                    {node.name}
                    <tspan
                      x={labelX}
                      dy={13}
                      className="fill-muted-foreground"
                      style={{ fontSize: 10, fontFamily: 'var(--font-mono, monospace)' }}
                    >
                      {fmtAmount(node.value ?? 0)}
                    </tspan>
                  </text>
                </g>
              )
            })}
          </g>

          {/* The hub's stacked bar already gives expenses/investments/
              transfers a clickable "whole lane" target (the segments
              above); income has no equivalent bar (the hub only stacks
              OUTFLOWS), so it gets its own small header over its column,
              in the same top gutter the hub's own label sits in. */}
          {incomeNode && (
            <g
              role="button"
              tabIndex={0}
              aria-label={nodeAriaLabel(t('reports.income'), totals.income)}
              className="cursor-pointer focus:outline-none focus-visible:opacity-70"
              onClick={() => handleLaneActivate('income')}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  handleLaneActivate('income')
                }
              }}
            >
              <text
                x={incomeNode.x0 ?? 8}
                y={10}
                textAnchor="start"
                className="fill-foreground"
                style={{ fontSize: 11, fontWeight: 600 }}
              >
                {t('reports.income')}
                <tspan
                  x={incomeNode.x0 ?? 8}
                  dy={13}
                  className="fill-muted-foreground"
                  style={{ fontSize: 10, fontFamily: 'var(--font-mono, monospace)', fontWeight: 400 }}
                >
                  {fmtAmount(totals.income)}
                </tspan>
              </text>
            </g>
          )}
        </svg>
      )}

      {/* Accessible table fallback — also a non-visual summary of the flows. */}
      <table className="sr-only">
        <caption>{t('reports.flowChartAria')}</caption>
        <thead>
          <tr>
            <th>{t('reports.category')}</th>
            <th>{t('reports.amount')}</th>
          </tr>
        </thead>
        <tbody>
          {rawNodes
            .filter((n) => n.side !== 'center')
            .map((n, i) => {
              const idx = rawNodes.indexOf(n)
              const value = layout?.nodes[idx]?.value ?? 0
              return (
                <tr key={i}>
                  <td>{n.name}</td>
                  <td>{fmtAmount(value)}</td>
                </tr>
              )
            })}
        </tbody>
      </table>

      <TransactionDrillDown filter={drillFilter} onClose={() => setDrillFilter(null)} />
    </div>
  )
}
