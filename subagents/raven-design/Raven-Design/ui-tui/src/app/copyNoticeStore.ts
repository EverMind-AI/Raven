/**
 * One transient notice above the composer.
 *
 * Copy-on-select writes the clipboard on every settled drag; reporting each
 * one as a transcript line stacks permanent rows. This holds a single notice
 * that replaces itself and clears after its duration, so the confirmation is
 * visible for a moment and then gone.
 */

import { atom } from 'nanostores'

export const $copyNotice = atom<null | string>(null)

let dismissTimer: null | ReturnType<typeof setTimeout> = null

export function showCopyNotice(text: string, ms: number): void {
  if (dismissTimer !== null) {
    clearTimeout(dismissTimer)
  }

  $copyNotice.set(text)
  dismissTimer = setTimeout(() => {
    dismissTimer = null
    $copyNotice.set(null)
  }, ms)
}

export function dismissCopyNotice(): void {
  if (dismissTimer !== null) {
    clearTimeout(dismissTimer)
    dismissTimer = null
  }

  $copyNotice.set(null)
}
