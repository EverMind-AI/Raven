/* Starting a task with a capability already named.
 *
 * Shared by both tabs of the capabilities page: the skills island offers it on
 * a skill's card and the plugins island on a server's. A
 * fresh conversation whose composer opens pre-filled, cursor at the end, ready
 * to complete -- which is why it lives beside the field it fills rather than
 * with either island.
 */

import { t } from '../../i18n/t'
import * as detail from '../../state/detail'

export function useInTask(promptKey: string, name: string): void {
  detail.close()
  ;(document.getElementById('newBtn') as HTMLElement).click()
  const ta = document.getElementById('ta') as HTMLTextAreaElement
  ta.value = t(promptKey, { name })
  ta.dispatchEvent(new Event('input', { bubbles: true }))
  ta.focus()
  ta.setSelectionRange(ta.value.length, ta.value.length)
}
