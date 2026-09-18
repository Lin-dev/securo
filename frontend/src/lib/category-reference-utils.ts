type CategoryReference = {
  id: string
  name: string
}

type RuleReference = {
  // `value` is a string for most actions and a list of lines for
  // `split_categories`; only the string form names a category here.
  actions: Array<{ op: string; value: unknown }>
}

export function findCategoryReference<T extends CategoryReference>(
  categories: readonly T[],
  categoryId: string,
): T | undefined {
  return categories.find((category) => category.id === categoryId)
}

export function getRuleCategoryId(rule: RuleReference): string | null {
  const action = rule.actions.find(
    (candidate) => candidate.op === 'set_category' && typeof candidate.value === 'string' && candidate.value,
  )
  return typeof action?.value === 'string' ? action.value : null
}

export function getRuleCategoryName<T extends CategoryReference>(
  rule: RuleReference,
  categories: readonly T[],
): string | null {
  const categoryId = getRuleCategoryId(rule)
  if (!categoryId) return null
  return findCategoryReference(categories, categoryId)?.name ?? null
}
