import { describe, expect, it } from 'vitest'

import { subscribeCopyOnSelect } from '../lib/copyOnSelect.js'

const flush = () => new Promise(resolve => setImmediate(resolve))

/** Stand-in for the ink selection bus. The real one needs a live Ink
 *  instance bound to a TTY, so the bus is faked and the assertions are on
 *  what this module decides to copy. */
function fakeSelection() {
  const listeners = new Set<() => void>()
  const copied: string[] = []
  const reported: string[] = []
  const state: {
    dragging: boolean
    present: boolean
    rawState?: unknown
    text: string
    version: number
    writeSucceeded: boolean
  } = {
    dragging: false,
    present: true,
    text: 'selected text',
    version: 1,
    writeSucceeded: true
  }

  return {
    copied,
    onCopied: (text: string) => reported.push(text),
    notify: () => {
      for (const cb of listeners) {
        cb()
      }
    },
    selection: {
      copySelectionNoClear: async () => {
        copied.push(state.text)

        return state.writeSucceeded ? state.text : ''
      },
      getState: (): unknown => state.rawState ?? { isDragging: state.dragging },
      hasSelection: () => state.present,
      subscribe: (cb: () => void) => {
        listeners.add(cb)

        return () => listeners.delete(cb)
      },
      version: () => state.version
    },
    reported,
    state
  }
}

describe('subscribeCopyOnSelect', () => {
  it('copies a settled selection on a non-macOS platform', () => {
    // The defect this closes: the subscription used to bail out on
    // `!isMac`, so a drag on Linux or Windows highlighted text and copied
    // nothing. This suite runs on whatever the host is -- on Linux (this
    // box, and CI) reaching the copy IS the proof the platform gate is gone.
    const bus = fakeSelection()

    subscribeCopyOnSelect(bus.selection)
    bus.notify()

    expect(bus.copied).toEqual(['selected text'])
  })

  it('leaves the clipboard alone while the drag is still moving', () => {
    // Copying every drag-move tick would overwrite the clipboard dozens of
    // times per selection and hand the user whatever partial span the mouse
    // happened to be crossing.
    //
    // Paired with the settled case on the same subscription: an assertion that
    // nothing was copied passes just as well when the bus was never wired up,
    // so the second half is what makes the first half mean anything.
    const bus = fakeSelection()

    bus.state.dragging = true
    subscribeCopyOnSelect(bus.selection)
    bus.notify()

    expect(bus.copied).toEqual([])

    bus.state.dragging = false
    bus.notify()

    expect(bus.copied).toEqual(['selected text'])
  })

  it('copies one selection version only once', () => {
    // The bus re-notifies on mutations that do not change the span, so
    // without version de-duping a single drag produced repeat clipboard
    // writes -- each one a fresh OSC 52 burst at the terminal.
    const bus = fakeSelection()

    subscribeCopyOnSelect(bus.selection)
    bus.notify()
    bus.notify()
    bus.notify()

    expect(bus.copied).toEqual(['selected text'])
  })

  it('copies again once the selection actually changes', () => {
    // The flip side of de-duping: a second drag must still reach the
    // clipboard, or copy-on-select works exactly once per session.
    const bus = fakeSelection()

    subscribeCopyOnSelect(bus.selection)
    bus.notify()

    bus.state.text = 'a later selection'
    bus.state.version = 2
    bus.notify()

    expect(bus.copied).toEqual(['selected text', 'a later selection'])
  })

  it('ignores a notification that carries no selection', () => {
    // Clearing the selection also notifies. Copying there would push an
    // empty string over whatever the user had on their clipboard.
    const bus = fakeSelection()

    bus.state.present = false
    subscribeCopyOnSelect(bus.selection)
    bus.notify()

    expect(bus.copied).toEqual([])

    bus.state.present = true
    bus.notify()

    expect(bus.copied).toEqual(['selected text'])
  })

  it('treats a bus with no readable state as not dragging', () => {
    // `useSelection().getState()` is typed `unknown` by the ambient
    // declaration, so this module has to narrow rather than assume. A bus
    // that reports nothing must not strand the selection uncopied.
    const bus = fakeSelection()

    bus.state.rawState = null
    subscribeCopyOnSelect(bus.selection)
    bus.notify()

    expect(bus.copied).toEqual(['selected text'])
  })

  it('stops copying once the subscription is disposed', () => {
    // The React effect returns this for cleanup; if it did not unsubscribe,
    // a remounted transcript would copy once per stale listener. Copy first so
    // the silence afterwards is attributable to the disposal.
    const bus = fakeSelection()

    const unsubscribe = subscribeCopyOnSelect(bus.selection)

    bus.notify()

    expect(bus.copied).toEqual(['selected text'])

    unsubscribe()
    bus.state.version = 2
    bus.notify()

    expect(bus.copied).toEqual(['selected text'])
  })
})

describe('subscribeCopyOnSelect reporting', () => {
  it('hands the copied text to the caller once the write lands', async () => {
    // Copy-on-select is silent by nature -- nothing on screen changes when a
    // drag ends. Without a report there is no way for the user to tell a
    // working copy from a dead one.
    const bus = fakeSelection()

    subscribeCopyOnSelect(bus.selection, bus.onCopied)
    bus.notify()
    await flush()

    expect(bus.reported).toEqual(['selected text'])
  })

  it('stays silent when no clipboard path took the text', async () => {
    // `copySelectionNoClear()` resolves to '' when nothing reached the
    // clipboard. Announcing a copy there would be the exact lie this change
    // set out to remove from `/copy`.
    const bus = fakeSelection()

    bus.state.writeSucceeded = false
    subscribeCopyOnSelect(bus.selection, bus.onCopied)
    bus.notify()
    await flush()

    expect(bus.copied).toEqual(['selected text'])
    expect(bus.reported).toEqual([])
  })

  it('reports once per selection, not once per notification', async () => {
    const bus = fakeSelection()

    subscribeCopyOnSelect(bus.selection, bus.onCopied)
    bus.notify()
    bus.notify()
    await flush()

    expect(bus.reported).toEqual(['selected text'])
  })

  it('works with no reporter attached', async () => {
    // The callback is optional; dropping it must not turn a copy into a crash.
    const bus = fakeSelection()

    subscribeCopyOnSelect(bus.selection)
    bus.notify()
    await flush()

    expect(bus.copied).toEqual(['selected text'])
  })
})
