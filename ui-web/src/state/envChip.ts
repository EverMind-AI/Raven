/* The environment chip under the composer.
 *
 * Everything runs on this machine, so the chip is a label rather than a switch:
 * one word and one dot, written once by the boot's own step list. What it
 * writes are two elements src/chrome/Dock.tsx renders, which is why it is a
 * writer rather than a rendered value -- the served chip already says this, and
 * nothing else ever changes it.
 */

export function setRuntime(): void {
  ;(document.getElementById('envName') as HTMLElement).textContent = '本机'
  ;(document.getElementById('envChip') as HTMLElement).querySelector('.led')!.className = 'led'
}
