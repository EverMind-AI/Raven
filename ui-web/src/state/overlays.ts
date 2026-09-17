/* What Escape takes back, and in which order.
 *
 * Fourteen layers can be on screen at once, and one key closes one of them.
 * Which one was a fourteen-branch if chain in the page's chrome: a list of
 * selectors read top to bottom, each branch returning so the ones below it
 * never ran.
 *
 * It is an ordered table here, and a table rather than a stack on purpose. A
 * stack would close whatever was raised last, and the chain's order is not
 * that order: the channel dialog opens over the entries page and closes first,
 * but the shared drawer opens over the dialog and closes second -- the comment
 * beside branch three said so in as many words. Each entry answers "am I open"
 * when the key arrives, so nothing here remembers a sequence.
 *
 * `id` is the text the chain tested, which is what the gate on this order
 * compares against (overlays.test.ts): a selector for the twelve layers with an
 * element, the predicate's own name for the two without one. `isOpen` is that
 * same test -- the attribute for the twelve, because that is what the chain
 * read and what the four islands and three stores that raise them write, and
 * the module's own answer for the settings dialog, which has been a flag in
 * state/settingsDialog.ts since C4, and for the running turn.
 *
 * The three capture-phase handlers each open sheet registers run before this
 * table and two of them act on Escape without stopping propagation, so one
 * Escape can both deny an approval and interrupt the turn behind it. That is
 * the behaviour, not an accident of where the listener sits.
 */

import { busy as turnBusy } from '../features/composer/turn'
import { islands } from '../islands'
import * as caps from './caps'
import * as detail from './detail'
import { close as closeImage, isOpen as imageOpen } from './lightbox'
import * as settingsDialog from './settingsDialog'
import { sources } from './sources'

export type Overlay = {
  /** The text the chain tested for this layer. */
  readonly id: string
  /** Asked afresh on every key, never cached. */
  isOpen(): boolean
  /** Exactly what the chain's branch did. */
  close(): void
}

/* The flag every page, veil and drawer is shown by, read off the element the
   way the chain read it. A layer whose markup is not in the document is not on
   screen, which is the one place this is laxer than the chain: that read threw
   instead, ending the key. */
const flagged = (id: string) => (): boolean => document.getElementById(id)?.dataset.open === 'true'

/* Two layers are taken back by pressing their own cancel button rather than by
   a verb: the button is the one path that runs the dialog's answer, and the
   sheet that owns it is another root's (state/confirm.ts, the cron island). */
const cancels = (id: string) => (): void => { document.getElementById(id)?.click() }

/** The fourteen, in the order Escape reaches them. */
export const ORDER: readonly Overlay[] = [
  { id: '.lightbox', isOpen: imageOpen, close: closeImage },
  { id: '#veil', isOpen: flagged('veil'), close: cancels('cfNo') },
  /* After the confirm veil, before the page: a dialog raised over the entry
     list is what Escape should take back first. */
  { id: '#connVeil', isOpen: flagged('connVeil'), close: () => islands.connections.closeDialog() },
  { id: '#detail', isOpen: flagged('detail'), close: detail.close },
  { id: '#jobVeil', isOpen: flagged('jobVeil'), close: cancels('jobNo') },
  { id: '#cronPage', isOpen: flagged('cronPage'), close: () => islands.cron.close() },
  { id: '#memPage', isOpen: flagged('memPage'), close: () => islands.memory.close() },
  { id: '#pbPage', isOpen: flagged('pbPage'), close: () => islands.playbooks.close() },
  { id: '#kbPage', isOpen: flagged('kbPage'), close: () => islands.knowledge.close() },
  { id: '#capsPage', isOpen: flagged('capsPage'), close: caps.close },
  { id: '#xaPage', isOpen: flagged('xaPage'), close: () => islands.xa.close() },
  { id: '#connPage', isOpen: flagged('connPage'), close: () => islands.connections.close() },
  { id: 'setIsOpen()', isOpen: settingsDialog.isOpen, close: settingsDialog.close },
  /* The last resort: with nothing on screen to take back, Escape interrupts
     the running turn. */
  { id: 'turn.busy()', isOpen: turnBusy, close: () => sources.composer?.stop() },
]

/** Closes the first layer that is open. Whether one was is the answer. */
export function dispatch(): boolean {
  for (const layer of ORDER) {
    if (!layer.isOpen()) continue
    layer.close()
    return true
  }
  return false
}
