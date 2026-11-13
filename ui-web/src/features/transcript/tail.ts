/* Owns whether the main transcript follows new output to its tail. */

let stuck = true

export const isStuck = (): boolean => stuck

export function setStuck(on: boolean): void {
  stuck = on
}

/* What going down does first: the transcript island (mount.tsx) mounts the
   newest turns here when its Lane Window stops short of them, so the end
   scrolled to is the conversation's rather than the window's. Handed in rather
   than imported, because this module sits below every caller of `down`. */
let reachTail: (() => void) | null = null

export function onDown(fn: () => void): void {
  reachTail = fn
}

export function down(): void {
  if (!stuck) return
  reachTail?.()
  const scroll = document.getElementById('scroll')
  /* A smooth scroll restarted by every token delta never reaches the tail. */
  if (scroll) scroll.scrollTo({ top: scroll.scrollHeight, behavior: 'instant' })
}

export function _resetForTests(): void {
  stuck = true
}
