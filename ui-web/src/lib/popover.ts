/* Where a panel opened from a settings row lands.
 *
 * The composer's own popovers -- the "+" menu, the workspace, the permission
 * modes -- hang off their chips with the stylesheet (`.chrome-anch` and `.pop` in
 * page.css) and measure nothing; the model picker opened from the composer
 * chip measures its own chip (features/model/ModelPicker.tsx). What is left
 * here is the one placement that has to know about a dialog: `anchorRow`
 * serves `components/ModelPicker`, whose last caller is the agents page.
 */

/* The settings dialog, which is the box a panel opened from a settings row has
   to stay inside. `.smodal` is `position: relative` and carries a transform of
   its own, so it is the containing block a `position: fixed` child resolves
   against as well as the box that clips it -- the same arrangement page.css
   records for the add-model drawer, "clipped by this dialog rather than
   floating over the whole window". */
/* Two boxes, not one. The settings dialog is where this started; the agents
   page opens the same picker from a detail sheet of its own (`.dpanel`), and a
   panel that only knew about `.smodal` fell through to the window and ran off
   the card's right edge. What the box decides is the WIDTH -- how far right the
   panel may reach. Vertically it is a floating layer and the window is its
   bound, which is what `anchorRow` says. */
const DIALOG = '.smodal, .dpanel'

/* How far a panel may reach, in viewport coordinates: the dialog it opened
   inside, else the window. */
function bounds(anchor: Element): { top: number; left: number; right: number; bottom: number } {
  const dialog = anchor.closest(DIALOG)
  if (dialog) {
    const r = dialog.getBoundingClientRect()
    return { top: r.top, left: r.left, right: r.right, bottom: r.bottom }
  }
  return { top: 0, left: 0, right: document.documentElement.clientWidth, bottom: document.documentElement.clientHeight }
}

/* Anchor a panel under a row, the way the prototype's own picker places itself
 * (`openPicker`'s `place`).
 *
 * Three rules, and they are not symmetric:
 *
 * - **Width** runs from the anchor's left edge to 20px inside the box it opened
 *   in, capped at 560 and floored at 320. So a panel never crosses the card's
 *   right edge and never gets so narrow that a model id has nowhere to go.
 * - **Downward first, and short rather than flipped.** A 400px panel that flips
 *   above a field halfway down a card covers the card it belongs to -- the
 *   reader loses the thing they opened it from. So it hangs below and gives up
 *   height to the shelf it has; only a shelf too shallow to read (under 65% of
 *   the full height) and a roomier ceiling send it up.
 * - **Vertically it is measured against the window, not the box.** The panel is
 *   a floating layer over everything, like the prototype's, so a card's bottom
 *   edge is not a wall -- clamping to it is what forced the flip.
 *
 * Fixed rather than absolute because the row may live inside a scroller
 * (`.spanels`, `overflow-y: auto`), which clips an absolutely positioned child:
 * a 400px popover opened from a row in the lower half lost up to 333 of those
 * pixels. A fixed child resolves against `.smodal` instead, which is above the
 * scroller, so the scroller has nothing to clip.
 *
 * The origin is measured rather than assumed. Which element a fixed box
 * resolves against depends on an ancestor having a transform, and `.smodal`'s
 * comes from an entry animation; reading where `top: 0` actually lands keeps
 * the arithmetic right whether that stays true or not.
 */
/* The panel's full height, and the shelf below which it would rather go up. */
const TALL = 400
const SHALLOW = 0.65

export function anchorRow(pop: HTMLElement, anchor: HTMLElement): void {
  const at = anchor.getBoundingClientRect()
  const box = bounds(anchor)
  const vw = document.documentElement.clientWidth
  const vh = document.documentElement.clientHeight
  pop.style.position = 'fixed'
  pop.style.right = 'auto'
  pop.style.bottom = 'auto'
  pop.style.top = '0'
  pop.style.left = '0'
  const origin = pop.getBoundingClientRect()
  const w = Math.max(320, Math.min(560, box.right - 20 - at.left))
  const below = vh - at.bottom - 14
  const above = at.top - 14
  const down = below >= TALL * SHALLOW || below >= above
  const h = Math.min(TALL, Math.max(160, down ? below : above))
  pop.style.width = `${w}px`
  pop.style.height = `${h}px`
  pop.style.left = `${Math.max(8, Math.min(at.left, vw - w - 12)) - origin.left}px`
  pop.style.top = `${(down ? at.bottom + 6 : at.top - 6 - h) - origin.top}px`
  /* Over the panel it hangs off, and under nothing else in the dialog: the same
     step page.css gives the add-model drawer. */
  pop.style.zIndex = '3'
}
