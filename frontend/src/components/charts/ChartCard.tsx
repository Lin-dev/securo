import { useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Table2 } from 'lucide-react'

import { cn } from '@/lib/utils'
import { Card, CardAction, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

export interface ChartCardColumn {
  key: string
  label: string
}

export interface ChartCardTable {
  columns: ChartCardColumn[]
  rows: Array<Record<string, ReactNode>>
}

export interface ChartCardProps {
  /** States the takeaway, not the metric name (P8) — e.g. "Spending is up 12% this month", not "Spending". */
  title: string
  subtitle?: string
  /** e.g. "Based on 94 days of data". */
  footnote?: string
  loading?: boolean
  empty?: boolean
  /** Table fallback shown by the "Show table" toggle. Omit to hide the toggle. */
  table?: ChartCardTable
  /** The chart itself. */
  children: ReactNode
  className?: string
}

/**
 * Chart frame shared by the kit: a takeaway title, loading/empty states, and
 * an accessible table fallback for anyone who can't or doesn't want the
 * visual chart. Children render the chart; ChartCard never touches privacy
 * mode or recharts itself.
 */
export function ChartCard({
  title,
  subtitle,
  footnote,
  loading = false,
  empty = false,
  table,
  children,
  className,
}: ChartCardProps) {
  const { t } = useTranslation()
  const [showTable, setShowTable] = useState(false)

  const canShowTable = Boolean(table) && !loading && !empty

  return (
    <Card className={cn('gap-3 py-5', className)}>
      <CardHeader className="gap-1">
        <CardTitle className="text-sm font-semibold text-foreground">{title}</CardTitle>
        {subtitle && <CardDescription>{subtitle}</CardDescription>}
        {canShowTable && (
          <CardAction>
            <button
              type="button"
              aria-pressed={showTable}
              onClick={() => setShowTable((v) => !v)}
              className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors cursor-pointer"
            >
              <Table2 size={13} aria-hidden="true" />
              {showTable ? t('charts.hideTable') : t('charts.showTable')}
            </button>
          </CardAction>
        )}
      </CardHeader>

      <div className="px-6">
        {loading ? (
          <Skeleton className="h-48 w-full" role="status" aria-label={t('common.loading')} />
        ) : empty ? (
          <p className="text-sm text-muted-foreground text-center py-12">{t('charts.empty')}</p>
        ) : showTable && table ? (
          <Table>
            <TableHeader>
              <TableRow>
                {table.columns.map((col) => (
                  <TableHead key={col.key}>{col.label}</TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {table.rows.map((row, i) => (
                <TableRow key={(row.id as string | undefined) ?? i}>
                  {table.columns.map((col) => (
                    <TableCell key={col.key}>{row[col.key]}</TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          children
        )}
      </div>

      {footnote && !loading && !empty && (
        <p className="px-6 text-[11px] text-muted-foreground">{footnote}</p>
      )}
    </Card>
  )
}
