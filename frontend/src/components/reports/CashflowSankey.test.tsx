import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'

import { CashflowSankey } from '@/components/reports/CashflowSankey'
import { admin, dashboard, transactions } from '@/lib/api'
import { renderWithProviders } from '@/test/utils'
import type { ReportCompositionItem } from '@/types'

vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }),
}))

vi.mock('@/lib/api', () => ({
  transactions: { list: vi.fn() },
  dashboard: { projectedTransactions: vi.fn() },
  admin: { accountingMode: vi.fn() },
}))

const navigate = vi.hoisted(() => vi.fn())
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigate,
}))

const WINDOW = { from: '2026-10-01', to: '2026-10-31' }

const composition: ReportCompositionItem[] = [
  { key: 'cat-salary', label: 'Salary', value: 1000, color: '#10B981', group: 'income' },
  { key: 'cat-rent', label: 'Rent', value: 400, color: '#F43F5E', group: 'expenses' },
]

// jsdom lays out nothing, so d3-sankey never gets a non-zero width unless
// every element's bounding box says otherwise -- give the chart's own
// container (and everything else) a deterministic size.
const realGetBoundingClientRect = HTMLElement.prototype.getBoundingClientRect

function renderChart(compositionOverride: ReportCompositionItem[] = composition) {
  return renderWithProviders(
    <CashflowSankey composition={compositionOverride} currency="USD" locale="en-US" window={WINDOW} />,
  )
}

describe('CashflowSankey', () => {
  beforeEach(() => {
    HTMLElement.prototype.getBoundingClientRect = () => DOMRect.fromRect({ width: 800, height: 400 })
    vi.mocked(transactions.list).mockResolvedValue({ items: [], total: 0 } as never)
    vi.mocked(dashboard.projectedTransactions).mockResolvedValue([])
    vi.mocked(admin.accountingMode).mockResolvedValue({ mode: 'cash' } as never)
    try { localStorage.removeItem('privacyMode') } catch { /* noop */ }
  })

  afterEach(() => {
    HTMLElement.prototype.getBoundingClientRect = realGetBoundingClientRect
    vi.resetAllMocks()
  })

  it('opens the drill-down for a category node on click, with the right category and window', async () => {
    renderChart()

    const node = await screen.findByRole('button', { name: 'Rent: $400' })
    fireEvent.click(node)

    await waitFor(() => {
      expect(transactions.list).toHaveBeenCalledWith(expect.objectContaining({
        category_id: 'cat-rent',
        from: WINDOW.from,
        to: WINDOW.to,
      }))
    })
    expect(await screen.findByRole('heading', { name: 'Rent' })).toBeInTheDocument()
  })

  it('opens the same drill-down on a keyboard Enter', async () => {
    renderChart()

    const node = await screen.findByRole('button', { name: 'Rent: $400' })
    node.focus()
    fireEvent.keyDown(node, { key: 'Enter' })

    await waitFor(() => {
      expect(transactions.list).toHaveBeenCalledWith(expect.objectContaining({
        category_id: 'cat-rent',
        from: WINDOW.from,
        to: WINDOW.to,
      }))
    })
  })

  it('also activates on a Space keydown', async () => {
    renderChart()

    const node = await screen.findByRole('button', { name: 'Rent: $400' })
    fireEvent.keyDown(node, { key: ' ' })

    await waitFor(() => {
      expect(transactions.list).toHaveBeenCalledWith(expect.objectContaining({ category_id: 'cat-rent' }))
    })
  })

  it('scopes an income/expense category\'s "View in Transactions" link to wealth_pnl_only', async () => {
    renderChart()

    const node = await screen.findByRole('button', { name: 'Salary: $1K' })
    fireEvent.click(node)

    const link = await screen.findByRole('link', { name: 'View in Transactions' })
    const params = new URLSearchParams(link.getAttribute('href')?.split('?')[1])
    expect(params.get('category_id')).toBe('cat-salary')
    expect(params.get('type')).toBe('credit')
    expect(params.get('wealth_pnl_only')).toBe('1')
    expect(params.get('from')).toBe(WINDOW.from)
    expect(params.get('to')).toBe(WINDOW.to)
  })

  it('masks the node aria-label in privacy mode', async () => {
    localStorage.setItem('privacyMode', 'true')
    renderChart()

    const node = await screen.findByRole('button', { name: /Rent:/ })
    expect(node.getAttribute('aria-label')).toBe('Rent: •••••')
    expect(node.getAttribute('aria-label')).not.toContain('$')
  })

  it('does not make surplus/deficit nodes focusable buttons', async () => {
    renderChart()

    // Salary (1000) > Rent (400): the leftover shows as "Surplus", a derived
    // balance with nothing to drill into.
    await screen.findByRole('button', { name: 'Rent: $400' })
    expect(screen.queryByRole('button', { name: /Surplus/ })).toBeNull()
  })

  it('navigates to Transactions with a comma-joined category_id list for a folded "Other" node', async () => {
    // 10 expense categories: collapse() keeps the 8 biggest and folds the
    // 2 smallest (cat-0 = 1, cat-1 = 2) into "Other".
    const many: ReportCompositionItem[] = Array.from({ length: 10 }, (_, i) => ({
      key: `cat-${i}`, label: `Cat ${i}`, value: i + 1, color: '#F43F5E', group: 'expenses',
    }))
    renderChart(many)

    const other = await screen.findByRole('button', { name: 'Other: $3' })
    fireEvent.click(other)

    expect(navigate).toHaveBeenCalledTimes(1)
    const url = navigate.mock.calls[0][0] as string
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('category_id')?.split(',').sort()).toEqual(['cat-0', 'cat-1'])
    // A local drill-down only ever scopes to one category, so this did NOT
    // open the slide-over -- the window is still carried, exactly.
    expect(params.get('from')).toBe(WINDOW.from)
    expect(params.get('to')).toBe(WINDOW.to)
    expect(screen.queryByRole('heading', { name: 'Other' })).toBeNull()
  })

  it('opens the income lane exactly (type=credit, wealth_pnl_only) from its header', async () => {
    renderChart()

    const incomeHeader = await screen.findByRole('button', { name: 'Income: $1K' })
    fireEvent.click(incomeHeader)

    const link = await screen.findByRole('link', { name: 'View in Transactions' })
    const params = new URLSearchParams(link.getAttribute('href')?.split('?')[1])
    expect(params.get('type')).toBe('credit')
    expect(params.get('wealth_pnl_only')).toBe('1')
    expect(params.has('category_id')).toBe(false)
  })

  it('opens the expenses lane exactly (type=debit, wealth_pnl_only) from the hub segment', async () => {
    renderChart()

    const expensesSegment = await screen.findByRole('button', { name: 'Expenses: $400' })
    fireEvent.click(expensesSegment)

    const link = await screen.findByRole('link', { name: 'View in Transactions' })
    const params = new URLSearchParams(link.getAttribute('href')?.split('?')[1])
    expect(params.get('type')).toBe('debit')
    expect(params.get('wealth_pnl_only')).toBe('1')
  })
})
