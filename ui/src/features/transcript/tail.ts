/* Owns whether the main transcript follows new output to its tail. */

let stuck = true

export const isStuck = (): boolean => stuck

export function setStuck(on: boolean): void {
  stuck = on
}

export function down(): void {
  if (!stuck) return
  const scroll = document.getElementById('scroll')
  /* A smooth scroll restarted by every token delta never reaches the tail. */
  if (scroll) scroll.scrollTo({ top: scroll.scrollHeight, behavior: 'instant' })
}

export function _resetForTests(): void {
  stuck = true
}
