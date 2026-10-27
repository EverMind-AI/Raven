/* Starting a task with its first message already written, and sending a
 * reader to the open conversation with a sentence to finish.
 *
 * A fresh conversation whose composer opens pre-filled, cursor at the end,
 * ready to edit and send -- which is why it lives beside the field it fills
 * rather than in any caller's domain. The agents sheet uses it to hand a failed
 * connect or test to Raven. A verb a click runs, not a hook: it reads no state
 * and renders nothing, and the `use` it used to carry had every caller
 * breaking `react-hooks/rules-of-hooks` on a call that is not a hook call.
 *
 * `askInChat` is the other door, for the settings dialog. A skill or an MCP
 * server is added by asking Raven in the chat (use_skill fetches the Skill Hub
 * bundle, the plugin tool connects the PlugHub entry), which no row of those
 * two pages can do -- so each page's "add" button puts a lead-in in the OPEN
 * conversation's composer, a sentence that ends where the reader's own
 * requirement starts on the next line, and sends nothing.
 */

import { t } from '../../i18n/t'
import * as detail from '../../state/detail'
import * as page from '../../state/page'
import * as settings from '../../state/settings'

const field = (): HTMLTextAreaElement => document.getElementById('ta') as HTMLTextAreaElement

/* The field filled, the cursor after the text, and the dock told. The input
   event is what the dock listens for (mount.tsx, then store.fieldInput): the
   field is sized to its text, Send wakes, and the text is parked as the
   conversation's own draft. */
function fill(ta: HTMLTextAreaElement, text: string): void {
  ta.value = text
  ta.dispatchEvent(new Event('input', { bubbles: true }))
  ta.focus()
  ta.setSelectionRange(ta.value.length, ta.value.length)
}

export function startTaskWith(promptKey: string, name: string, vars: Record<string, string> = {}): void {
  detail.close()
  ;(document.getElementById('newBtn') as HTMLElement).click()
  fill(field(), t(promptKey, { ...vars, name }))
}

/* The dialog closes and a module page that covers the chat is put away; the
   conversation itself is left as it was, a running turn included (Enter on a
   busy turn queues, as it always does). The lead-in ends with a line break,
   so the cursor lands on a line of its own for the reader's requirement: in
   this field Enter sends, and the break is one they could only type with
   Shift. What was already typed is kept, with the lead-in after it: opening
   the dialog must not cost a draft. */
export function askInChat(text: string): void {
  settings.close()
  if (page.get() !== null) page.show(null)
  const ta = field()
  const typed = ta.value
  const lead = `${text}\n`
  fill(ta, typed.trim() ? `${typed.replace(/\s+$/, '')}\n\n${lead}` : lead)
}
