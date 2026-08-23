/**
 * iTerm-style copy-on-select for the TUI transcript.
 *
 * A TUI that enables mouse tracking owns the drag, so the terminal never
 * builds a native selection and its own copy shortcut has nothing to copy.
 * Writing the span to the clipboard as soon as the drag settles is what makes
 * a TUI selection copyable at all, on every platform.
 */

/** The slice of the ink selection bus this needs. Structural rather than the
 *  full `useSelection()` return so the module stays testable without a live
 *  Ink instance. */
export type CopyOnSelectSelection = {
  copySelectionNoClear: () => Promise<string>
  getState: () => unknown
  hasSelection: () => boolean
  subscribe: (cb: () => void) => () => void
  version: () => number
}

/**
 * Copy each settled selection to the clipboard, keeping the highlight, and
 * hand the copied text to `onCopied` once a clipboard path actually took it.
 * Returns the bus unsubscribe, so a React effect can return it directly.
 *
 * `copySelectionNoClear()` resolves to '' when no path reached the clipboard,
 * so the callback fires on a real write only.
 *
 * Subscribes to the bus rather than going through `useSyncExternalStore` so
 * the transcript does not re-render on every drag-move tick, and de-dupes on
 * the selection version because the bus also notifies for mutations that
 * leave the span unchanged.
 */
/** The ambient `useSelection()` declaration types the bus state as
 *  `unknown`, so read the one field this needs instead of asserting a shape.
 *  Anything unreadable counts as "not dragging" -- the drag is over far more
 *  often than the state is missing, and guessing the other way would drop the
 *  copy entirely. */
function isDragging(state: unknown): boolean {
  return typeof state === 'object' && state !== null && (state as { isDragging?: unknown }).isDragging === true
}

export function subscribeCopyOnSelect(selection: CopyOnSelectSelection, onCopied?: (text: string) => void): () => void {
  let lastCopiedVersion = -1

  return selection.subscribe(() => {
    if (!selection.hasSelection()) {
      return
    }

    if (isDragging(selection.getState())) {
      return
    }

    const version = selection.version()

    if (version === lastCopiedVersion) {
      return
    }

    lastCopiedVersion = version
    void selection.copySelectionNoClear().then(text => {
      if (text) {
        onCopied?.(text)
      }
    })
  })
}
