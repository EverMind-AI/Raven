/** How much each desk tab held when the reader last looked at it, per conversation.

What is new in a tab is the difference between what it holds and this. The
numbers only mean anything beside the lists they count, and those are per
conversation -- so these are too, filed under the same session key and read back
whenever that conversation is the open one. There is no park and no restore:
nothing carries them across a switch, because the switch is the key changing.

Their own slot rather than a field on the desk's layout note, for one reason
that was a bug first: `remember()` publishes the whole note -- panes, solo,
active, tab -- from whatever the desk holds at that instant, and a mark moves
during a resume, when the desk holds nothing yet and the note is about to be
read. A mark writing through that path landed `open: []` over the panes the
replay was about to restore. A mark is news about a mark; it now writes nowhere
else.
*/

import { slot } from '../../shell/persist'
import { current as currentSession } from '../../shell/session'

import type { DeskMarks, DeskTab } from './deskTypes'

const EMPTY: DeskMarks = { diff: 0, deliverables: 0, agents: 0 }

const KEPT = slot<DeskMarks>('marks', 1)

/* A draft has no key to file under: its marks live for as long as the draft is
   on screen, which is the same life the draft itself has. */
let draftMarks: DeskMarks = { ...EMPTY }

const read = (): DeskMarks => {
  const key = currentSession()
  if (!key) return draftMarks
  return { ...EMPTY, ...(KEPT.read(key) || {}) }
}

export const get = (): DeskMarks => read()

export const of_ = (tab: DeskTab): number => read()[tab]

export function set(tab: DeskTab, value: number): boolean {
  const now = read()
  if (now[tab] === value) return false
  const next = { ...now, [tab]: value }
  const key = currentSession()
  if (key) KEPT.write(key, next)
  else draftMarks = next
  return true
}

/* A session switch does not need to clear anything -- the key changed, and the
   next read is the new conversation's own row. This is for the resets that mean
   "this conversation starts over": the draft's own numbers, and the tests. */
export function reset(): void {
  draftMarks = { ...EMPTY }
}

export function _clearForTests(): void {
  draftMarks = { ...EMPTY }
  KEPT.clear()
}
