import type { ReactNode } from 'react'

interface SettingsCardProps {
  title: string
  description?: string
  /** A header-right affordance, e.g. "Use defaults". */
  action?: ReactNode
  children: ReactNode
}

/** The shared card shell every settings section uses, matching the app's
 *  existing settings-screen style (see `timezone-settings.tsx`). */
export function SettingsCard({ title, description, action, children }: SettingsCardProps) {
  return (
    <section className="rounded-xl border border-border/60 bg-card overflow-hidden">
      <div className="flex items-center justify-between gap-3 border-b border-border/40 px-5 py-4">
        <div>
          <h2 className="text-sm font-semibold text-foreground">{title}</h2>
          {description && <p className="mt-0.5 text-sm text-muted-foreground">{description}</p>}
        </div>
        {action}
      </div>
      <div className="space-y-4 p-5">{children}</div>
    </section>
  )
}
