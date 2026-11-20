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

import { slot } from '../../lib/persist'
import { current as currentSession } from '../../lib/session'

import type { DeskSeen, DeskTab } from './types'

const EMPTY = (): DeskSeen => ({ diff: [], deliverables: [], tasks: [] })

const KEPT = slot<DeskSeen>('seen', 1)

/* Per tab, newest kept. An id is a path or a handle -- tens of bytes -- and a
   conversation that delivers more than this has a shelf nobody scrolls to the
   bottom of; the cap is here so a month-old tab cannot grow the store without
   bound. It bounds a tracked slice rather than the tab: `tracked` is the part
   the record can speak for, and the badge counts that same slice, so a count
   the record cannot clear is never shown. A real session does outgrow it -- one
   with two dozen delegated tasks reached roughly nine hundred changed files --
   which is why the overflow is a stated limit and not an impossible case. */
export const CAP = 300

/* The ids one tab's record can speak for: the first cap of its list, the end
   its newest items sit on. `unseen` counts only these and `seeTab` marks only
   these. Counting the whole tab against a bounded record was the mismatch: the
   overflow could never be retired, and a caller marking after every render
   never settled. */
export const tracked = (ids: readonly string[]): string[] => ids.slice(0, CAP)

/* A draft has no key to file under: what it has been shown lives as long as the
   draft is on screen, which is the same life the draft itself has. */
let draftSeen: DeskSeen = EMPTY()

/* The same value storage holds, per conversation, kept here so the answer does
   not depend on a read. `mark` decides from what it reads, and a storage that
   refuses the write -- private mode, over quota -- would read back the previous
   record, so the ids just marked would look new on every render and the
   palette's every-render mark would never settle. Storage stays the
   across-reload record; this is the same value until the next reload. */
const marked = new Map<string, DeskSeen>()

const read = (): DeskSeen => {
  const key = currentSession()
  if (!key) return draftSeen
  const kept = marked.get(key)
  if (kept) return kept
  const stored = { ...EMPTY(), ...(KEPT.read(key) || {}) }
  marked.set(key, stored)
  return stored
}

const listOf = (all: DeskSeen, tab: DeskTab): string[] =>
  Array.isArray(all[tab]) ? all[tab] : []

/* Everything this tab has been shown, as the set the counters ask. */
export const of_ = (tab: DeskTab): Set<string> => new Set(listOf(read(), tab))

/* Adds, never removes: seen is a one-way fact about the reader. Answers whether
   the record actually changed, so a caller running on every render (the palette
   marks the tab it is drawing) only publishes when there is news.

   The caller's own order comes first, because it is the tab's own order:
   marking a tab's tracked slice then writes exactly that slice back, and the
   second call changes nothing. Appending to the kept list instead let the cap
   evict an id that was still inside the slice, which read back as new on the
   next call -- a record that could never settle once a tab outgrew it. */
export function mark(tab: DeskTab, ids: readonly string[]): boolean {
  const now = read()
  const kept = listOf(now, tab)
  const known = new Set(kept)
  const next = [...new Set([...ids.filter((id) => id), ...kept])].slice(0, CAP)
  if (next.length === known.size && next.every((id) => known.has(id))) return false
  const written = { ...now, [tab]: next }
  const key = currentSession()
  if (key) {
    marked.set(key, written)
    KEPT.write(key, written)
  } else {
    draftSeen = written
  }
  return true
}

/* A session switch does not need to clear anything -- the key changed, and the
   next read is the new conversation's own row. This is for the resets that mean
   "this conversation starts over": the draft's own record, and the tests. */
export function reset(): void {
  draftSeen = EMPTY()
}

export function _resetForTests(): void {
  draftSeen = EMPTY()
  marked.clear()
  KEPT.clear()
}
