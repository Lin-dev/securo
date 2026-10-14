import React, { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { monthLabel } from '@/lib/month-utils'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { categories as categoriesApi, categoryGroups as groupsApi, budgets as budgetsApi, wealth } from '@/lib/api'
import { extractApiError } from '@/lib/api-errors'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { DeleteConfirmationDialog } from '@/components/delete-confirmation-dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog'
import type { Budget } from '@/types'
import { Plus, CalendarIcon } from 'lucide-react'
import { format } from 'date-fns'
import { Popover, PopoverTrigger, PopoverContent } from '@/components/ui/popover'
import { MonthPicker } from '@/components/ui/monthpicker'
import { PageHeader } from '@/components/page-header'
import { CategoryIcon } from '@/components/category-icon'
import { useAuth } from '@/contexts/auth-context'
import { useWorkspace } from '@/contexts/workspace-context'
import { resolveDateFnsLocale } from '@/lib/date-fns-locale'
import { findCategoryReference } from '@/lib/category-reference-utils'
import { monthWindow } from '@/lib/chart-period'
import { transactionsUrl } from '@/lib/drilldown'
import { SuggestionsPanel } from '@/components/budgets/suggestions-panel'
import { SpendingCapCard } from '@/components/budgets/spending-cap-card'
import { BudgetRow } from '@/components/budgets/budget-row'

function currentMonth() {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
}

function SectionCard({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-card rounded-xl border border-border shadow-sm overflow-hidden">
      {children}
    </div>
  )
}
function SectionHeader({ title, action }: { title: string; action?: React.ReactNode }) {
  return (
    <div className="px-4 sm:px-5 py-4 border-b border-border flex flex-wrap items-center justify-between gap-2">
      <p className="text-sm font-semibold text-foreground">{title}</p>
      {action}
    </div>
  )
}

export default function BudgetsPage() {
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  const { user } = useAuth()
  const { canWrite, hasModule } = useWorkspace()
  const wealthEnabled = hasModule('wealth')
  const userCurrency = user?.preferences?.currency_display ?? 'USD'
  const locale = useDisplayLocale()
  const queryClient = useQueryClient()
  const [selectedMonth, setSelectedMonth] = useState(currentMonth)
  const [monthCalOpen, setMonthCalOpen] = useState(false)
  const dateFnsLocale = resolveDateFnsLocale(i18n.resolvedLanguage ?? i18n.language)
  const monthParam = `${selectedMonth}-01`
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<Budget | null>(null)
  const [deletingBudget, setDeletingBudget] = useState<Budget | null>(null)
  // `null` means "no explicit choice yet": the panel defaults open until the
  // person has budgets, then defaults collapsed (plan S7). Once they toggle
  // it themselves, that choice wins regardless of how many budgets exist.
  const [suggestionsExpandedOverride, setSuggestionsExpandedOverride] = useState<boolean | null>(null)

  const { data: budgetsList } = useQuery({
    queryKey: ['budgets', selectedMonth],
    queryFn: () => budgetsApi.list(monthParam),
  })

  const { data: comparisonList } = useQuery({
    queryKey: ['budgets', 'comparison', selectedMonth],
    queryFn: () => budgetsApi.comparison(monthParam),
  })

  const { data: suggestionsData } = useQuery({
    queryKey: ['wealth', 'budget-suggestions'],
    queryFn: wealth.budgetSuggestions,
    enabled: wealthEnabled,
  })

  const { data: paceData, isLoading: paceLoading } = useQuery({
    queryKey: ['wealth', 'budget-pace'],
    queryFn: wealth.budgetPace,
    enabled: wealthEnabled,
  })

  const suggestionsExpanded = suggestionsExpandedOverride ?? !(budgetsList && budgetsList.length > 0)

  const { data: categoriesList } = useQuery({
    queryKey: ['categories'],
    queryFn: categoriesApi.list,
  })

  // Budgets can point at a hidden default category. The picker below only
  // offers visible ones, but existing rows still have to name what they budget.
  const { data: allCategoriesList } = useQuery({
    queryKey: ['categories', 'management'],
    queryFn: categoriesApi.listIncludingHidden,
  })

  const { data: groupsList } = useQuery({
    queryKey: ['category-groups'],
    queryFn: groupsApi.list,
  })

  const createMutation = useMutation({
    mutationFn: (data: { category_id: string; amount: number; month: string; is_recurring?: boolean }) =>
      budgetsApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['budgets'] })
      setDialogOpen(false)
      toast.success(t('budgets.created'))
    },
    onError: () => toast.error(t('common.error')),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, amount }: { id: string; amount: number }) =>
      budgetsApi.update(id, { amount }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['budgets'] })
      setDialogOpen(false)
      setEditing(null)
      toast.success(t('budgets.updated'))
    },
    onError: () => toast.error(t('common.error')),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => budgetsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['budgets'] })
      setDeletingBudget(null)
      toast.success(t('budgets.deleted'))
    },
    onError: (err: unknown) => {
      toast.error(extractApiError(err, t('common.error')))
    },
  })

  const applySuggestionsMutation = useMutation({
    mutationFn: (categoryIds: string[]) => wealth.applyBudgetSuggestions({ category_ids: categoryIds }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['budgets'] })
      queryClient.invalidateQueries({ queryKey: ['wealth'] })
      toast.success(t('budgetsPace.suggestions.resultToast', { created: result.created, skipped: result.skipped }))
    },
    onError: (err: unknown) => toast.error(extractApiError(err, t('common.error'))),
  })

  const capMutation = useMutation({
    mutationFn: (body: { spending_cap?: number; clear_spending_cap?: boolean }) => wealth.updateSettings(body),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['wealth'] })
      toast.success(t(variables.clear_spending_cap ? 'budgetsPace.cap.cleared' : 'budgetsPace.cap.saved'))
    },
    onError: (err: unknown) => toast.error(extractApiError(err, t('common.error'))),
  })

  const displayCategories = allCategoriesList ?? categoriesList ?? []

  const comparisonByCategory = new Map((comparisonList ?? []).map((c) => [c.category_id, c]))

  function goToCategory(categoryId: string) {
    navigate(transactionsUrl({ ...monthWindow(monthParam), categoryId }))
  }

  const uiLocale = i18n.resolvedLanguage ?? i18n.language
  const monthTitle = monthLabel(selectedMonth, uiLocale).replace(/^\w/, c => c.toUpperCase())

  return (
    <div>
      <PageHeader
        section={t('budgets.title')}
        title={monthTitle}
        action={
          <div className="flex items-center gap-1">
            <button
              className="h-8 w-8 flex items-center justify-center rounded-lg border border-border bg-card text-muted-foreground hover:border-border hover:text-foreground transition-all text-base"
              onClick={() => {
                const [y, m] = selectedMonth.split('-').map(Number)
                const d = new Date(y, m - 2, 1)
                setSelectedMonth(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`)
              }}
            >‹</button>
            <Popover open={monthCalOpen} onOpenChange={setMonthCalOpen}>
              <PopoverTrigger asChild>
                <button
                  type="button"
                  className="inline-flex items-center justify-center gap-2 border border-border rounded-lg px-3 py-1.5 text-sm bg-card text-foreground hover:bg-muted/50 transition-all cursor-pointer min-w-[180px]"
                >
                  <CalendarIcon className="size-3.5 text-muted-foreground" />
                  {monthTitle}
                </button>
              </PopoverTrigger>
              <PopoverContent align="center" className="w-auto p-0">
                <MonthPicker
                  locale={dateFnsLocale}
                  selectedMonth={new Date(`${selectedMonth}-01T00:00:00`)}
                  onMonthSelect={(date) => {
                    if (!date) return
                    setSelectedMonth(format(date, 'yyyy-MM'))
                    setMonthCalOpen(false)
                  }}
                />
              </PopoverContent>
            </Popover>
            <button
              className="h-8 w-8 flex items-center justify-center rounded-lg border border-border bg-card text-muted-foreground hover:border-border hover:text-foreground transition-all text-base"
              onClick={() => {
                const [y, m] = selectedMonth.split('-').map(Number)
                const d = new Date(y, m, 1)
                setSelectedMonth(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`)
              }}
            >›</button>
          </div>
        }
      />

      {wealthEnabled && suggestionsData && suggestionsData.suggestions.length > 0 && (
        <SuggestionsPanel
          data={suggestionsData}
          currency={userCurrency}
          locale={locale}
          expanded={suggestionsExpanded}
          onToggleExpanded={() => setSuggestionsExpandedOverride(!suggestionsExpanded)}
          onCreate={(categoryIds) => applySuggestionsMutation.mutate(categoryIds)}
          isPending={applySuggestionsMutation.isPending}
        />
      )}

      <SectionCard>
        <SectionHeader
          title={t('budgets.title')}
          action={
            canWrite ? (
              <Button size="sm" className="gap-1.5 h-8" onClick={() => { setEditing(null); setDialogOpen(true) }}>
                <Plus size={13} /> {t('budgets.add')}
              </Button>
            ) : undefined
          }
        />
        {budgetsList && budgetsList.length > 0 ? (
          <div>
            {budgetsList.map((budget) => {
              const category = findCategoryReference(displayCategories, budget.category_id)
              return (
                <BudgetRow
                  key={budget.id}
                  budget={budget}
                  comparison={comparisonByCategory.get(budget.category_id)}
                  categoryName={category?.name ?? budget.category_id}
                  categoryIcon={<CategoryIcon icon={category?.icon} color={category?.color} size="sm" />}
                  currency={userCurrency}
                  locale={locale}
                  canWrite={canWrite}
                  onEdit={() => { setEditing(budget); setDialogOpen(true) }}
                  onDelete={() => setDeletingBudget(budget)}
                  onNavigate={() => goToCategory(budget.category_id)}
                />
              )
            })}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground text-center py-10">{t('budgets.empty')}</p>
        )}
      </SectionCard>

      {wealthEnabled && (
        <SpendingCapCard
          pace={paceData}
          isLoading={paceLoading}
          currency={userCurrency}
          locale={locale}
          typicalMonthlySpending={
            suggestionsData?.typical_monthly_spending != null ? Number(suggestionsData.typical_monthly_spending) : null
          }
          onSetCap={(amount) => capMutation.mutate({ spending_cap: amount })}
          onClearCap={() => capMutation.mutate({ clear_spending_cap: true })}
          isSaving={capMutation.isPending}
        />
      )}

      <Dialog open={dialogOpen} onOpenChange={() => { setDialogOpen(false); setEditing(null) }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editing ? t('budgets.edit') : t('budgets.add')}</DialogTitle>
          </DialogHeader>
          <form
            key={editing?.id ?? 'new'}
            onSubmit={(e) => {
              e.preventDefault()
              const formData = new FormData(e.currentTarget)
              if (editing) {
                updateMutation.mutate({
                  id: editing.id,
                  amount: parseFloat(formData.get('amount') as string),
                })
              } else {
                const isRecurring = formData.get('is_recurring') === 'on'
                createMutation.mutate({
                  category_id: formData.get('category_id') as string,
                  amount: parseFloat(formData.get('amount') as string),
                  month: monthParam,
                  is_recurring: isRecurring,
                })
              }
            }}
            className="space-y-4"
          >
            {!editing && (
              <>
                <div className="space-y-2">
                  <Label>{t('budgets.category')}</Label>
                  <select
                    name="category_id"
                    className="w-full border border-border rounded-lg px-3 py-2 text-sm bg-card text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                    required
                  >
                    <option value="">{t('budgets.selectCategory')}</option>
                    {groupsList?.map((group) => (
                      <optgroup key={group.id} label={group.name}>
                        {group.categories.map((cat) => (
                          <option key={cat.id} value={cat.id}>{cat.name}</option>
                        ))}
                      </optgroup>
                    ))}
                    {categoriesList?.filter((c) => !c.group_id).map((cat) => (
                      <option key={cat.id} value={cat.id}>{cat.name}</option>
                    ))}
                  </select>
                </div>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input type="checkbox" name="is_recurring" className="rounded border-border" />
                  <span className="text-sm text-foreground">{t('budgets.repeatEveryMonth')}</span>
                </label>
              </>
            )}
            <div className="space-y-2">
              <Label>{t('budgets.amount')}</Label>
              <Input
                name="amount"
                type="number"
                step="0.01"
                defaultValue={editing?.amount?.toString() ?? ''}
                required
              />
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => { setDialogOpen(false); setEditing(null) }}>
                {t('common.cancel')}
              </Button>
              <Button type="submit" disabled={createMutation.isPending || updateMutation.isPending}>
                {t('common.save')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <DeleteConfirmationDialog
        open={!!deletingBudget}
        title={t('budgets.confirmDeleteTitle')}
        description={t(
          deletingBudget?.is_recurring
            ? 'budgets.confirmDeleteRecurringDescription'
            : 'budgets.confirmDeleteDescription',
          {
            name: findCategoryReference(displayCategories, deletingBudget?.category_id ?? '')?.name
              ?? t('budgets.category'),
          },
        )}
        isPending={deleteMutation.isPending}
        onClose={() => setDeletingBudget(null)}
        onConfirm={() => deletingBudget && deleteMutation.mutate(deletingBudget.id)}
      />
    </div>
  )
}
