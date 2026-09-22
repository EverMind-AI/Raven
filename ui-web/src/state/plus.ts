/* The "+" at the left of the composer bar (#plusBtn) and the menu it opens
 * (#plusPop): the two things a message can carry besides its words -- a file,
 * a deck template -- behind one button, in a draft and in a conversation alike.
 *
 * A store rather than a writer: <Plus/> (src/chrome/PlusMenu.tsx) renders the
 * button and the menu from it. What decides is here: which of the two rows the
 * composer source can honour, and whether the menu is up. `draw` is asked for
 * by the composer's own repaint (features/composer/store.ts goPaint), which
 * runs at boot and on every change of the dock, because the source it reads is
 * installed by the boot.
 *
 * The button is hidden only where neither row can be honoured -- a page with
 * no upload and no templates has nothing to put behind a "+".
 */

import { ds } from './sources'
import { makeStore } from './store'

export interface PlusState {
  /** Whether the button is drawn at all: at least one row can be honoured. */
  readonly shown: boolean
  /** Which rows the menu can offer, as the composer source answers. */
  readonly upload: boolean
  readonly template: boolean
  /** Up or down: the popover's data-open and the button's aria-expanded. */
  readonly open: boolean
}

const shut: PlusState = { shown: false, upload: false, template: false, open: false }
const store = makeStore<PlusState>(shut)

export const { get, subscribe } = store

export function set(next: PlusState): void {
  const now = get()
  if (next.shown === now.shown && next.upload === now.upload
    && next.template === now.template && next.open === now.open) return
  store.set(next)
}

/* Guarded like state/workdir.ts's reads of its seams: the composer source is
   installed by the boot, and a draw that runs ahead of it (or a test that
   never installs one) has nothing to offer rather than a throw to make. */
function offers(): { upload: boolean; template: boolean } {
  try {
    const src = ds('composer')
    return { upload: !!src.upload, template: !!src.templates }
  } catch {
    return { upload: false, template: false }
  }
}

/* The button and the menu, from the composer source. A menu left up over a
   button that lost both rows is closed with them. */
export function draw(): void {
  const { upload, template } = offers()
  const shown = upload || template
  set({ shown, upload, template, open: shown && get().open })
}

export function open(): void {
  if (!get().shown) return
  set({ ...get(), open: true })
}

export function close(): void {
  set({ ...get(), open: false })
}

export const isOpen = (): boolean => get().open

/* The button toggles rather than opens, for the reason the permission chip's
   does: the popover has no close button of its own. */
export function toggle(): void {
  if (isOpen()) close()
  else open()
}

/* Test seam only: the menu outlives a case's DOM. */
export function _resetForTests(): void {
  set(shut)
}
