/* Display names for agent kinds, shared by the connection and conversation lists. */

import { t } from '../i18n/t'

export function agentKindLabel(kind?: string | null): string {
  switch (kind) {
    case 'builtin': return t('gui.agent.kind_builtin')
    case 'acp': return t('gui.agent.kind_acp')
    case 'openai': return t('gui.agent.kind_openai')
    case 'cli': return t('gui.agent.kind_cli')
    default: return kind || t('gui.deleg.d_agent')
  }
}
