/* Starting a task with its first message already written.
 *
 * A fresh conversation whose composer opens pre-filled, cursor at the end,
 * ready to edit and send -- which is why it lives beside the field it fills
 * rather than in any caller's domain. The agents sheet uses it to hand a failed
 * connect or test to Raven. A verb a click runs, not a hook: it reads no state
 * and renders nothing, and the `use` it used to carry had every caller
 * breaking `react-hooks/rules-of-hooks` on a call that is not a hook call.
 */

import { t } from '../../i18n/t'
import * as detail from '../../state/detail'

export function startTaskWith(promptKey: string, name: string, vars: Record<string, string> = {}): void {
  detail.close()
  ;(document.getElementById('newBtn') as HTMLElement).click()
  const ta = document.getElementById('ta') as HTMLTextAreaElement
  ta.value = t(promptKey, { ...vars, name })
  ta.dispatchEvent(new Event('input', { bubbles: true }))
  ta.focus()
  ta.setSelectionRange(ta.value.length, ta.value.length)
}
