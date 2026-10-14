import { useTranslation } from 'react-i18next'

import { Badge } from '@/components/ui/badge'
import type { WealthSettingSource } from '@/types/wealth'

/** "Default" vs "Yours" — where a settings value came from (plan P6). */
export function SourceBadge({ source }: { source: WealthSettingSource }) {
  const { t } = useTranslation()
  return (
    <Badge variant={source === 'user' ? 'secondary' : 'outline'}>
      {t(source === 'user' ? 'wealthSettings.source.user' : 'wealthSettings.source.default')}
    </Badge>
  )
}
