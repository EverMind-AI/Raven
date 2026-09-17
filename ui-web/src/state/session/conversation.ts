/* The conversation: the empty state's flag, the reader's own message, and the
 * one row raven speaks about itself in.
 *
 * The renderer is the transcript island (features/transcript/); what is here
 * is the empty-state flag -- which is a
 * whole layout rather than a message, so it belongs to the chat column -- and
 * the three verbs the session runtime and its stages drive the island by.
 */

import { islands } from '../../features/registry'
import { I18N } from '../../i18n/t'

import type { NoteHandle } from '../../features/transcript/types'

/* The empty state is the composer itself, moved to the visual centre -- no
   mark, no facts, no title. `unpitch` lifts the flag again. */
export function pitch(): void {
  const c = document.querySelector('.chat') as HTMLElement | null
  if (c) c.dataset.fresh = '1'
}

/* Leaving the empty state. Its own verb, because the flag drives a whole layout
   -- the wordmark, the crew, a centred composer 81px above where the dock sits,
   and an opaque card instead of the glass one -- so every path out of it has to
   lift the flag at the same moment, and there are four of them (content
   arriving, a stored conversation replaying, and either kind of switch through
   resetView). */
export function unpitch(): void {
  const c = document.querySelector('.chat') as HTMLElement | null
  if (c) delete c.dataset.fresh
}

/* The composer appends an "[attachments]" note plus "- path" bullets for the
   model; the reader gets chips instead. Parsed against both language variants
   of the note, since history may have been written under the other one. */
export function splitAtts(text: string): { body: string; atts: string[] } {
  const notes = Object.values((I18N.ui['gui.att.note'] ?? {}) as Record<string, string>)
  for (const note of notes) {
    if (!note) continue
    const ix = text.lastIndexOf(`\n\n${note}\n`)
    if (ix < 0) continue
    const tail = text.slice(ix + note.length + 3).split('\n')
    if (!tail.length || !tail.every((l) => !l.trim() || /^- /.test(l))) continue
    return {
      body: text.slice(0, ix),
      atts: tail.filter((l) => /^- /.test(l)).map((l) => l.slice(2).trim()),
    }
  }
  return { body: text, atts: [] }
}

export function ask(text: string, when?: string): void {
  unpitch()
  islands.transcript.setStuck(true)
  /* The bubble, its attachment chips and its footer are the island's. */
  islands.transcript.ask(text, when)
}

/* Writes what the row shows and what it can give back, together; `row` is the
   island handle `noteRow` returned. */
export function noteSay(row: NoteHandle, label: string, detail: string): void {
  row.set(label, detail)
}

/* The one row for everything raven says about itself in the transcript, so a
   new kind of notice cannot arrive wearing its own shape. `label` leads, the
   detail follows after a `.`, and the whole thing is one line: a failure and a
   framework note differ only in colour. Options: `quiet` for a framework note
   (no failure happened, so no red) and `retry`, which is the caller's -- this
   row cannot know what should happen next. Omit it and no action appears. */
export function noteRow(
  label: string,
  detail: string,
  /* `host` is passed by one caller and has never been forwarded, for the reason
     below; it is in the type because that call site is real. */
  opts?: { quiet?: boolean; retry?: (() => void) | null; host?: unknown } | null
): NoteHandle {
  const o = opts ?? {}
  /* The row is an island segment; the handle keeps the two verbs the page still
     uses on it (noteSay's set, compressNow's remove). `host` needs no
     forwarding: the island's main lane IS the #stage transcript, and a
     delegated pane draws its own notes from its own record. */
  return islands.transcript.note(label, detail, {
    quiet: !!o.quiet,
    retry: typeof o.retry === 'function' ? o.retry : null,
  })
}
