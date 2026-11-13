/* Owns whether the main transcript follows new output to its tail. */

let stuck = true

export const isStuck = (): boolean => stuck

export function setStuck(on: boolean): void {
  stuck = on
}

/* Whether the end of a scroll box is the conversation's end. A Lane Window
   that stops short of the tail ends at a turn, so the box's own bottom is only
   the window's: reaching it must not turn tail-follow on. One predicate for
   every reader of that geometry, because `scrollHeight` is the window's once
   the stage mounts part of the lane (`TranscriptPage`'s `stageRows`). */
export function atConversationEnd(
  box: { scrollHeight: number; scrollTop: number; clientHeight: number },
  winTo: number,
): boolean {
  return !winTo && box.scrollHeight - box.scrollTop - box.clientHeight < 4
}

/* What going down does first: the transcript island (mount.tsx) mounts the
   newest turns here when its Lane Window stops short of them, so the end
   scrolled to is the conversation's rather than the window's. Handed in rather
   than imported: this module is the leaf the session and composer layers sit
   above, and importing the transcript store here would point that dependency
   back up. Not a cycle today -- a layering choice. */
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
