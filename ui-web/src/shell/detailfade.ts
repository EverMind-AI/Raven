/* Letting the shared #detail drawer finish leaving before its card is dropped.
 *
 * The drawer fades over a fifth of a second (page.css: `.detail` transitions
 * opacity), and every module used to unmount its card in the same tick the
 * flag flipped. So what actually faded was an empty strip where a card had
 * been -- measured on the skills card, 630px of content became a 43px bar one
 * millisecond after the close, and that bar is what the eye caught on the way
 * out. Nothing was wrong with the card or the fade; they simply were not the
 * same thing for those 220 milliseconds.
 *
 * The state drop is therefore deferred by the fade's own length. `stale` is
 * asked again at the end because a reader may open another card inside that
 * window: the second open has already written the state, and this must not
 * wipe theirs on the way past.
 */

/* Kept in step with `.detail`'s opacity transition in page.css. A little
   longer than the transition, so the drop lands after the last painted frame
   rather than in the middle of it. */
const FADE_MS = 260

export function dropAfterFade(drop: () => void, stale: () => boolean): void {
  window.setTimeout(() => {
    if (stale()) return
    drop()
  }, FADE_MS)
}
