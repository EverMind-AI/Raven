/** Which of each desk tab's items the reader has already seen, per conversation.

Identities, not a count, and the difference is the whole reason this file
replaced the watermark it grew out of. A reader opens ONE of three delivered
files from the conversation's own card, never touching the desk: that file is
not new to them any more, and the shelf has to stop counting it. A watermark --
"the tab held three when you last looked" -- has no way to say WHICH one.
Subtracting one gets the total right by luck and stays wrong: one arrival plus
one read cancel out, and the tab goes quiet holding something unread.

So a tab's news is `its items minus these`, and the two grains fall out of the
same set: looking at a tab marks everything in it, opening one thing marks that
thing. This is also how the demo layer has always counted changes
(`changes.filter(c => !c.seen)`), so the two halves of the page now say it the
same way, and the `Math.max(0, ...)` a watermark needed for a list that shrank
is gone -- a set cannot go negative.

Their own slot rather than a field on the desk's layout note, for one reason
that was a bug first: `remember()` publishes the whole note -- panes, solo,
active, tab -- from whatever the desk holds at that instant, and a mark moves
during a resume, when the desk holds nothing yet and the note is about to be
read. A mark writing through that path landed `open: []` over the panes the
replay was about to restore. A mark is news about a mark; it writes nowhere
else.
*/

import { slot } from '../../shell/persist'
import { current as currentSession } from '../../shell/session'

import type { DeskSeen, DeskTab } from './deskTypes'

const EMPTY = (): DeskSeen => ({ diff: [], deliverables: [], agents: [] })

const KEPT = slot<DeskSeen>('seen', 1)

/* Per tab, newest kept. An id is a path or a handle -- tens of bytes -- and a
   conversation that delivers more than this has a shelf nobody scrolls to the
   bottom of; the cap is here so a month-old tab cannot grow the store without
   bound. Overflowing drops the oldest, which can make one very old item look
   new again: the cheapest wrong answer available, and reachable only past a
   count no real session has produced. */
const CAP = 300

/* A draft has no key to file under: what it has been shown lives as long as the
   draft is on screen, which is the same life the draft itself has. */
let draftSeen: DeskSeen = EMPTY()

const read = (): DeskSeen => {
  const key = currentSession()
  if (!key) return draftSeen
  return { ...EMPTY(), ...(KEPT.read(key) || {}) }
}

const listOf = (all: DeskSeen, tab: DeskTab): string[] =>
  Array.isArray(all[tab]) ? all[tab] : []

/* Everything this tab has been shown, as the set the counters ask. */
export const of_ = (tab: DeskTab): Set<string> => new Set(listOf(read(), tab))

/* Adds, never removes: seen is a one-way fact about the reader. Answers whether
   anything was actually added, so a caller running on every render (the palette
   marks the tab it is drawing) only publishes when there is news. */
export function mark(tab: DeskTab, ids: readonly string[]): boolean {
  const now = read()
  const kept = listOf(now, tab)
  const known = new Set(kept)
  const fresh = ids.filter((id) => id && !known.has(id))
  if (!fresh.length) return false
  const next = { ...now, [tab]: [...kept, ...fresh].slice(-CAP) }
  const key = currentSession()
  if (key) KEPT.write(key, next)
  else draftSeen = next
  return true
}

/* A session switch does not need to clear anything -- the key changed, and the
   next read is the new conversation's own row. This is for the resets that mean
   "this conversation starts over": the draft's own record, and the tests. */
export function reset(): void {
  draftSeen = EMPTY()
}

export function _clearForTests(): void {
  draftSeen = EMPTY()
  KEPT.clear()
}
