/* ══ module 1b: the conversation ══════════════════════════════════ */

import { islands } from '../../islands'
import { I18N } from './010-kernel.js'

function pitch() {
  /* The empty state is the composer itself, moved to the visual centre --
     no mark, no facts, no title. unpitch() lifts the flag again. */
  const c = document.querySelector('.chat');
  if (c) c.dataset.fresh = '1';
}

/* Leaving the empty state. Its own element, because the flag drives a whole
   layout -- the wordmark, the crew, a centred composer 81px above where the
   dock sits, and an opaque card instead of the glass one -- so every path out
   of it has to lift the flag at the same moment, and there are four of them
   (content arriving, a stored conversation replaying, and either kind of
   switch through resetView). */
function unpitch() {
  const c = document.querySelector('.chat');
  if (c) delete c.dataset.fresh;
}

/* The composer appends an "[attachments]" note plus "- path" bullets for the
   model; the reader gets chips instead. Parsed against both language variants
   of the note, since history may have been written under the other one. */
function splitAtts(text) {
  const notes = Object.values(I18N.ui['gui.att.note'] || {});
  for (const note of notes) {
    if (!note) continue;
    const ix = text.lastIndexOf('\n\n' + note + '\n');
    if (ix < 0) continue;
    const tail = text.slice(ix + note.length + 3).split('\n');
    if (!tail.length || !tail.every((l) => !l.trim() || /^- /.test(l))) continue;
    return {
      body: text.slice(0, ix),
      atts: tail.filter((l) => /^- /.test(l)).map((l) => l.slice(2).trim()),
    };
  }
  return { body: text, atts: [] };
}

function ask(text, when) {
  unpitch();
  islands.transcript.setStuck(true);
  /* The bubble, its attachment chips and its footer are the island's. */
  islands.transcript.ask(text, when);
}

/* Writes what the row shows and what it can give back, together; `row` is
   the island handle noteRow returned. */
function noteSay(row, label, detail) {
  row.set(label, detail);
}

/* The one row for everything raven says about itself in the transcript, so a
   new kind of notice cannot arrive wearing its own shape. `label` leads, the
   detail follows after a `·`, and the whole thing is one line: a failure and a
   framework note differ only in colour. Options: `quiet` for a framework note
   (no failure happened, so no red) and `retry`, which is the caller's -- this
   row cannot know what should happen next. Omit it and no action appears. */
function noteRow(label, detail, opts) {
  const o = opts || {};
  /* The row is an island segment now; the handle keeps the two verbs the
     shell still uses on it (noteSay's set, compressNow's remove). `host`
     needs no forwarding: the island's main lane IS the #stage transcript,
     and a delegated pane draws its own notes from its own record. */
  return islands.transcript.note(label, detail,
    { quiet: !!o.quiet, retry: typeof o.retry === 'function' ? o.retry : null });
}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {

}

export { pitch, unpitch, splitAtts, ask, noteSay, noteRow }
