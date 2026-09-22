/* The model chip's repaint signal.
 *
 * The chip itself is a component now (src/chrome/ModelChip.tsx), rendered from
 * the model store's `current`. What that store cannot see is a change in what
 * the chip should SAY about the same model -- the provider list refreshed and
 * now carries a label, or the language flipped -- so the paths that used to
 * write the label by id bump this instead, and the chip re-reads the list.
 * `label` keeps its name because two call sites are held to it by literal: the
 * settings chrome's provider refresh (features/settings/wire.ts) and the
 * language repaint (state/lang/effects.ts).
 */

import { makeStore } from '../../state/store'

export const paint = makeStore(0)

/** Asks the chip to re-read the provider list and the catalogue. */
export function label(): void {
  paint.set(paint.get() + 1)
}
