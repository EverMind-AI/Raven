/* A panel as tall as its content that glides to a new height when the content
 * changes, rather than jumping to it.
 *
 * CSS alone cannot do it: the panel's height is `auto` before the change and
 * after it, so no computed value changes for a transition to animate. Instead
 * the height is taken twice -- the one last laid out, and the one the new
 * content takes -- and the panel is pinned at the first and handed the second,
 * which the stylesheet's `[data-glide]` rule transitions. When it lands the pin
 * is dropped and the panel is its content's height again.
 *
 * The change is read in the MutationObserver's microtask: after the DOM moved,
 * before the frame is painted, so the jump is never on screen. A change while a
 * glide runs re-aims it from wherever it has got to. `offsetHeight` rather than
 * the bounding box: the panel's own open lift scales it, and a scaled height
 * pinned as a layout height would shrink it by that much.
 */

/* Kept in step with the `[data-glide]` transition in page.css; the timer is a
   floor under `transitionend`, which does not fire for a panel hidden mid-way. */
export const GLIDE_MS = 240

export interface Glide {
  /* The content about to arrive is a new card, not a change to this one: take
     its height as the starting point once it has been laid out, with no glide. */
  settle(): void
  stop(): void
}

export function glide(panel: HTMLElement, content: HTMLElement, live: () => boolean): Glide {
  const still = typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-reduced-motion: reduce)') : null
  let last = panel.offsetHeight
  let quiet = false
  let timer: number | undefined
  let frame: number | undefined

  const land = (): void => {
    window.clearTimeout(timer)
    timer = undefined
    panel.style.height = ''
    delete panel.dataset.glide
  }
  const natural = (): number => {
    panel.style.height = ''
    return panel.offsetHeight
  }

  const changed = (): void => {
    const pinned = panel.style.height !== ''
    const from = pinned ? panel.offsetHeight : last
    const to = natural()
    last = to
    if (quiet || !live() || still?.matches || Math.abs(to - from) < 1) {
      land()
      return
    }
    panel.style.height = `${from}px`
    void panel.offsetHeight
    panel.dataset.glide = 'true'
    panel.style.height = `${to}px`
    window.clearTimeout(timer)
    timer = window.setTimeout(land, GLIDE_MS + 80)
  }

  const ended = (e: TransitionEvent): void => {
    if (e.target === panel && e.propertyName === 'height') land()
  }
  /* A window resize moves the panel's ceiling, not its content: the next glide
     starts from the height the panel has now. */
  const resized = (): void => {
    if (panel.style.height === '') last = panel.offsetHeight
  }

  const watch = new MutationObserver(changed)
  watch.observe(content, { childList: true, subtree: true, attributes: true, characterData: true })
  panel.addEventListener('transitionend', ended)
  window.addEventListener('resize', resized)

  return {
    settle(): void {
      quiet = true
      land()
      if (frame !== undefined) window.cancelAnimationFrame(frame)
      frame = window.requestAnimationFrame(() => {
        frame = undefined
        quiet = false
        last = natural()
      })
    },
    stop(): void {
      watch.disconnect()
      panel.removeEventListener('transitionend', ended)
      window.removeEventListener('resize', resized)
      if (frame !== undefined) window.cancelAnimationFrame(frame)
      land()
    },
  }
}
