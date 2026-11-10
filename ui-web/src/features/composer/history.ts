/* The composer's input history: what the arrow keys walk, stored with the TUI.

 * The entries are the ones in ``$RAVEN_HOME/.raven_history`` -- the file the
 * terminal reads and writes -- so a prompt sent from either front end is
 * recallable in the other. This module owns only the recall state and the
 * page's in-memory copy: the file's format is the TUI's
 * (``ui-tui/src/lib/history.ts``) and the parsing happens on the server, once
 * (``raven/input_history.py``). A page never touches the file.

 * The state is module-level rather than in the composer store on purpose. The
 * field is an uncontrolled textarea and every keystroke runs through
 * ``fieldInput()``; a React store write per arrow would repaint the box the
 * reader is typing in, which is the same reason the field is not controlled
 * (``chrome/Dock.tsx``). Nothing outside this module renders from it.

 * Semantics are the TUI's, with one deliberate difference: ``down`` also
 * requires the caret to be on the last logical line, so a multi-line entry is
 * walked with the arrows first and left only at its edge. ``up`` already
 * behaves that way in the terminal; ``down`` there only checks
 * ``historyIdx !== null``, which is the asymmetry this side does not copy.
 */

import * as wire from './source'

/** The TUI's in-memory cap: `ui-tui/src/lib/history.ts` slices to this. */
export const MAX = 1000

/* Oldest first, as the file stores them. */
let items: string[] = []
/* Which entry is on screen, or null while the field is an ordinary draft. */
let index: number | null = null
/* What the field held when history browsing began; restored by `down` past
   the newest entry. Not the persisted draft (`raven.gui.drafts`) -- that one
   is "unsent text across a reload", this one lives only inside one browse. */
let draft = ''

let started: Promise<void> | null = null
/* Appends made before the first list answer landed, merged into it so a send
   during the boot is not dropped from the page's copy. */
let settled = false
const early: string[] = []

const push = (text: string): void => {
  if (items.at(-1) === text) return
  items.push(text)
  if (items.length > MAX) items.splice(0, items.length - MAX)
}

/**
 * Load the page's copy once. Resolves even when the gateway is absent or
 * refuses: an empty history is a working page, and the arrows fall through to
 * the browser's own caret movement.
 */
export function init(): Promise<void> {
  if (started) return started
  started = (async () => {
    try {
      const answer = await wire.historyList(MAX)
      items = answer.entries.slice(-MAX)
    } catch {
      items = []
    }
    early.forEach(push)
    early.length = 0
    settled = true
  })()
  return started
}

/** The page's copy, oldest first. Read by tests and by nothing else. */
export const entries = (): readonly string[] => items

/**
 * Add a sent prompt to the page's copy and, in the background, to the shared
 * file. Blank text is a message with only attachments, which the TUI does not
 * record either. A failed write leaves the in-memory copy alone: the page can
 * still recall it until a reload.
 */
export function append(text: string): void {
  const trimmed = text.trim()
  if (!trimmed) return
  push(trimmed)
  if (!settled) early.push(trimmed)
  try {
    void wire.historyAppend(trimmed).catch(() => { /* best effort */ })
  } catch {
    /* No gateway installed: the page keeps its own copy. */
  }
}

/**
 * One arrow press: -1 is up, 1 is down. Returns the value to write into the
 * field, or null when the key changes nothing and the browser should keep it.
 *
 * An empty string is a real answer -- restoring an empty draft -- so callers
 * must test `!== null`, not truthiness.
 */
export function navigate(dir: -1 | 1, value: string): string | null {
  if (!items.length) {
    index = null
    return null
  }
  if (index !== null && index >= items.length) index = items.length - 1

  if (dir < 0) {
    if (index === null) {
      draft = value
      index = items.length - 1
      return items[index] ?? null
    }
    if (index <= 0) return null
    index -= 1
    return items[index] ?? null
  }

  if (index === null) return null
  if (index + 1 >= items.length) {
    index = null
    return draft
  }
  index += 1
  return items[index] ?? null
}

/**
 * Typing over a recalled entry turns the edited text into the draft: the next
 * arrow must not step away and lose it. Same predicate as the TUI's
 * `shouldDetachEditedHistoryInput`.
 */
export function detachEdited(value: string): void {
  if (index === null) return
  if (index < items.length && items[index] === value) return
  draft = value
  index = null
}

/** A conversation switch or a send: browsing ends here. */
export function reset(): void {
  index = null
  draft = ''
}

/* Test seam only: module state survives between cases. */
export function _resetForTests(): void {
  items = []
  index = null
  draft = ''
  started = null
  settled = false
  early.length = 0
}
