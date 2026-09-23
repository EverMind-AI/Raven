/* The whole-page redraw a language pick asks for.
 *
 * Only what is DRAWN rather than rendered is here: every component on the page
 * reads the catalogue itself and re-renders on the same notification, islands
 * included (each `<Domain>App` subscribes, which is what
 * scripts/gates/island-lang.test.mjs holds it to), so an island that needs no
 * more than a re-render needs no step.
 *
 * What cannot be a re-render is here: the rail's own draw and the capabilities
 * page's, the three stores whose draw commits a field their component renders
 * (the permission chip, the context ring), the model label --
 * the one step left that writes an element by id -- the settings dialog's
 * epoch, the nav flyout's marks, the transcript's per-lane version bump, the
 * composer's queue, the shared drawer and the one reload.
 *
 * One subscriber rather than a call beside each `lang.set`: the rollback path in
 * state/lang/pick.ts would otherwise have to remember to redraw a second time,
 * and a pick that fails to persist has to leave the page in exactly the state
 * the pick before it did.
 *
 * It runs after the rendered half has committed: the markup follows the
 * catalogue first, and only then is everything drawn from JavaScript redrawn.
 * `lang.onApplied` is what that ordering is (state/lang/store.ts).
 *
 * The order is load-bearing in one place: the reload at the end reopens the
 * conversation from disk, so it has to be last, and the guard in front of it is
 * what keeps it off a streaming turn.
 */

import { drawQueue as queueDraw, turn } from '../../features/composer/mount'
import { label as modelLabel } from '../../features/model/chip'
import { draw as sessionDraw } from '../../features/rail/store'
import { redraw as redrawSettings } from '../../features/settings/store'
import { redraw as redrawTranscript } from '../../features/transcript/mount'
import { current as sessionCurrent } from '../../lib/session'
import { draw as drawCtx } from '../ctxChip'
import * as detail from '../detail'
import { draw as drawPerm } from '../perm'
import { isDraft } from '../session/registry'
import { open as sessionOpen, sess } from '../session/rows'
import * as lang from './store'

export function repaint(): void {
  sessionDraw()
  modelLabel()
  drawPerm()
  drawCtx()
  /* The settings dialog's own epoch, which is more than a re-render: the panes
     are keyed on it, so a flip rebuilds each one rather than diffing a tree
     whose words all moved. */
  redrawSettings()
  /* The shared drawer is closed rather than redrawn: it is not on any page, so
     nothing above reaches it, and every one of its five openers would have to
     hand back the subject it was drawn from. Left open it would sit in the old
     language over a page now in the new one, which reads worse than losing the
     place -- and only the settings dialog, which the flip is made from, is
     above it. */
  detail.close()
  /* The transcript island re-renders its catalogue words (verbs, fold headers,
     footers) in place -- which is also what covers a turn still streaming,
     where the reload below must not run. */
  redrawTranscript()
  queueDraw()
  /* The words baked into stored segments (note labels, phrased previews) come
     back right on a rebuild from disk. Skipped while a turn is streaming:
     re-opening the session mid-turn would cut the stream off. */
  if (!isDraft() && sessionCurrent() && !turn.busy()) {
    const row = sess(sessionCurrent())
    if (row) void sessionOpen(row)
  }
}

/** Subscribes the redraw once. src/main.tsx is the only caller. */
export function install(): void {
  lang.onApplied(repaint)
}
