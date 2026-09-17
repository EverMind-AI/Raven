/* The whole-page redraw a language pick asks for.
 *
 * A hand-written list of nineteen calls (`redrawAll`) is what this was.
 * Everything the catalogue reaches that is DRAWN rather than rendered is
 * in it: the regions src/App.tsx renders read the catalogue themselves and
 * redraw on the same notification, so what is left here is the eleven islands,
 * the four chrome writers, the shared drawer and the one reload.
 *
 * One subscriber rather than a call beside each `lang.set`: the rollback path in
 * state/lang/pick.ts would otherwise have to remember to redraw a second time,
 * and a pick that fails to persist has to leave the page in exactly the state
 * the pick before it did.
 *
 * It runs after the rendered half has committed, which is the order applyI18n
 * had -- the markup was rewritten and only then was everything drawn from
 * JavaScript redrawn. `lang.onApplied` is what that ordering is (state/lang/store.ts).
 *
 * The list is in redrawAll's order, and the order is load-bearing in one place:
 * the reload at the end reopens the conversation from disk, so it has to be
 * last, and the guard in front of it is what keeps it off a streaming turn.
 */

import { islands } from '../../features/registry'
import { drawQueue as queueDraw, turn } from '../../features/composer/mount'
import { label as modelLabel } from '../../features/model/chip'
import { draw as sessionDraw } from '../../features/rail/store'
import { draw as drawCtx } from '../ctxChip'
import { draw as drawFoot } from '../foot'
import { draw as drawPerm } from '../perm'
import { current as sessionCurrent } from '../../lib/session'
import * as caps from '../caps'
import * as detail from '../detail'
import * as lang from './store'
import { isDraft } from '../session/registry'
import { open as sessionOpen, sess } from '../session/rows'

export function repaint(): void {
  sessionDraw()
  drawFoot()
  modelLabel()
  drawPerm()
  drawCtx()
  /* Every module page, not just the open one: a hidden page keeps its old DOM,
     so it would still be in the previous language when reopened. */
  islands.settings.redraw()
  try { caps.draw() } catch { /* extensions not loaded yet */ }
  try { islands.connections.redraw() } catch { /* channels not loaded yet */ }
  try { islands.cron.redraw() } catch { /* schedules not loaded yet */ }
  try { islands.xa.redraw() } catch { /* agents not loaded yet */ }
  try { islands.memory.redraw() } catch { /* memory not loaded yet */ }
  try { islands.knowledge.redraw() } catch { /* knowledge not loaded yet */ }
  try { islands.playbooks.redraw() } catch { /* playbooks not loaded yet */ }
  /* The More rows are redrawn on each open, so only a group standing open at
     the moment of the flip keeps the old names. */
  islands.nav.draw()
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
  islands.transcript.redraw()
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
