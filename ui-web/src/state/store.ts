/* One store shape for the whole page, and the notify contract inside it.
 *
 * A store here is a module singleton holding one value, because the page is one
 * page and its imperative callers are not React: the Escape order, the language
 * effects and the session pipeline all read and write these from outside any
 * component, so a hook-shaped store would have nowhere to live.
 *
 * The notify is synchronous and it is a contract rather than a detail. A writer
 * commits and then hands over -- the confirm dialog raises the veil after the
 * question lands and focuses #cfNo, the capabilities page draws and then lets an
 * island fill what it drew, the page's own root is committed with flushSync at
 * boot (src/main.tsx) for the same reason. React would otherwise commit in a
 * later task, and the statement after the write would read yesterday's DOM. So
 * `set` notifies inside flushSync: listeners run synchronously, and the React
 * roots among them have painted by the time `set` returns.
 *
 * `_resetForTests` puts the value back and leaves the subscribers alone. A
 * mounted root owns its own subscription and React takes it back when it
 * unmounts, so dropping them here would unwire the first reset and leave every
 * case after it watching nothing. It does not notify either: a test seam is not
 * a write the page made.
 *
 * The state a store holds is a value, never a function -- `set` tells the two
 * apart by asking, the way React's own setter does.
 */

import { flushSync } from 'react-dom'

export interface Store<T> {
  /** The value, as it stands. For useSyncExternalStore's getSnapshot. */
  get(): T
  /** The next value, or a function from the current one. Notifies. */
  set(next: T | ((prev: T) => T)): void
  /** For useSyncExternalStore: called on every write. */
  subscribe(fn: () => void): () => void
  /** Test seam only: the value is the module's and outlives a case's DOM. */
  _resetForTests(): void
}

export function makeStore<T>(initial: T): Store<T> {
  let value = initial
  const listeners = new Set<() => void>()

  return {
    get: () => value,

    set(next) {
      value = typeof next === 'function' ? (next as (prev: T) => T)(value) : next
      /* Nothing to notify is nothing to flush: a forced commit with no
         listener behind it would only bring unrelated pending work forward. */
      if (!listeners.size) return
      /* A copy, because a listener may unsubscribe itself while it runs. */
      flushSync(() => {
        for (const fn of [...listeners]) fn()
      })
    },

    subscribe(fn) {
      listeners.add(fn)
      return () => {
        listeners.delete(fn)
      }
    },

    _resetForTests() {
      value = initial
    },
  }
}
