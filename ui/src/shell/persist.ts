/* View state that survives a reload.
 *
 * What a slot holds is where the reader WAS -- the id of the run a sheet was
 * watching, the paths of the windows on the desk -- and never what the page
 * drew. The content is read back from the gateway on the way in (`dag.get` for
 * a run, the file readers for a pane), because the gateway is where it is true.
 * A copy kept here would come back as a graph frozen at the moment of the
 * reload, with nodes pinned `running` that have long since finished, and a
 * confidently wrong picture is worse than an empty dock.
 *
 * `sessionStorage`, not `localStorage`, and the difference is the whole point.
 * The ask is that a RELOAD returns to what was on screen -- a reconnect after
 * an upgrade replaces the page the same way -- which is exactly this store's
 * lifetime: kept across a refresh, gone when the tab closes, and never
 * inherited by a tab that was opened fresh. It is also per tab, so two windows
 * on one gateway cannot write over each other's layout; a second window is a
 * second view, and there is nothing to reconcile between them. The reader's
 * PREFERENCES (pane widths, theme, drafts) are the other kind of state and stay
 * in localStorage where they are.
 *
 * Written straight through rather than debounced. A slot's payload is a handful
 * of ids -- under a kilobyte -- and a reload is not a kill: debouncing would buy
 * nothing and would need a flush hook on pagehide to stay correct.
 */

const PREFIX = 'raven.gui.view.'

/* One session's entry, stamped so the cap below can drop the coldest. */
interface Entry<T> {
  at: number
  d: T
}

interface Box<T> {
  v: number
  s: Record<string, Entry<T>>
}

/* A slot holding one value rather than one per conversation: which
   conversation was open is a fact about the tab, not about any of them. Written
   through the same box so the version, the storage guard and the tab scoping
   are stated once. */
export interface Only<T> {
  read(): T | null
  write(value: T): void
  clear(): void
}

export function only<T>(name: string, version: number): Only<T> {
  const s = slot<T>(name, version, 1)
  const KEY = '_'
  return {
    read: () => s.read(KEY),
    write: (value) => s.write(KEY, value),
    clear: () => s.forget(KEY),
  }
}

export interface Slot<T> {
  read(key: string): T | null
  write(key: string, value: T): void
  forget(key: string): void
  /* Everything this slot holds, for a test that must not inherit the last
     one's writes. Not a product path: nothing in the page has a reason to
     drop every conversation's layout at once. */
  clear(): void
}

/* A cap rather than an eviction policy worth the name: the entries are keyed by
   session, and a machine left open for a week must not carry the layout of
   every conversation ever opened in it. Same shape and the same reason as the
   composer's draft cap. */
const CAP = 20

export function slot<T>(name: string, version: number, cap: number = CAP): Slot<T> {
  const store = PREFIX + name

  const empty = (): Box<T> => ({ v: version, s: {} })

  const box = (): Box<T> => {
    let raw: string | null = null
    try {
      raw = sessionStorage.getItem(store)
    } catch {
      /* Storage may be unavailable in private mode. */
    }
    if (!raw) return empty()
    let parsed: unknown = null
    try {
      parsed = JSON.parse(raw)
    } catch {
      return empty()
    }
    if (!parsed || typeof parsed !== 'object') return empty()
    const kept = parsed as Box<T>
    /* A version bump means the shape changed under what is stored, and the
       stored copy carries no way to say which shape it is. Dropped whole rather
       than migrated: this is one reload's worth of layout, and re-earning it
       costs the reader a click. */
    if (kept.v !== version || !kept.s || typeof kept.s !== 'object') return empty()
    return { v: version, s: kept.s }
  }

  const put = (next: Box<T>): void => {
    const keys = Object.keys(next.s)
    if (keys.length > cap) {
      keys
        .sort((a, b) => (next.s[a]?.at || 0) - (next.s[b]?.at || 0))
        .slice(0, keys.length - cap)
        .forEach((key) => delete next.s[key])
    }
    try {
      if (Object.keys(next.s).length) sessionStorage.setItem(store, JSON.stringify(next))
      /* An empty box is removed rather than written: "nothing was open" and
         "nothing has been stored yet" are the same state to every reader, and
         one spelling of it means the quota holds no dead keys. */
      else sessionStorage.removeItem(store)
    } catch {
      /* Private mode, or over quota. A layout that could not be stored is a
         layout the next reload does without -- there is nothing to report. */
    }
  }

  return {
    read: (key) => {
      const entry = box().s[key]
      return entry ? entry.d : null
    },
    write: (key, value) => {
      const next = box()
      next.s[key] = { at: Date.now(), d: value }
      put(next)
    },
    forget: (key) => {
      const next = box()
      if (!(key in next.s)) return
      delete next.s[key]
      put(next)
    },
    clear: () => {
      try {
        sessionStorage.removeItem(store)
      } catch {
        /* private mode */
      }
    },
  }
}
