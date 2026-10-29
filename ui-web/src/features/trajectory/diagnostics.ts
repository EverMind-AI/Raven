/* TEMPORARY DIAGNOSTICS -- remove once the React #185 loop is found.
 *
 * React's limit is on NESTED updates inside one task, not on writes per
 * second: a loop of fifty renders completes in a few milliseconds and never
 * looks busy. So the counter is per task -- reset when the task yields -- and
 * the stack printed at the limit names the writer that is repeating.
 */

const LIMIT = 30

let counts = new Map<string, number>()
let resetScheduled = false

export function count(site: string): void {
  const n = (counts.get(site) ?? 0) + 1
  counts.set(site, n)
  if (n === LIMIT) {
    console.warn(`[traj-hot] ${site}: ${LIMIT} writes in one task\n${new Error('caller').stack ?? ''}`)
  }
  if (!resetScheduled) {
    resetScheduled = true
    setTimeout(() => { counts = new Map(); resetScheduled = false }, 0)
  }
}

/** The repo's module-state gate: this module keeps counters between calls. */
export function _resetForTests(): void {
  counts = new Map()
  resetScheduled = false
}
