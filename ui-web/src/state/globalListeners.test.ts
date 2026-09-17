// @vitest-environment happy-dom
/* The order the page registers its document- and window-level listeners in.
 *
 * Five of them are capture-phase and read an event before the element the
 * reader touched ever sees it, and inside one phase the first handler
 * registered runs first -- so this sequence decides, for every pair of
 * listeners that answer the same event, which one answers it first. It used to
 * be an accident of which module main.tsx called first; the table below is
 * what it is instead.
 *
 * The recorder replaces addEventListener rather than wrapping it, so nothing is
 * left registered on the document afterwards and the cases can run in any
 * order. It also sees only what installGlobalListeners() itself registers,
 * which is what the case is about: the three registrations the page makes that
 * are NOT product code -- react-dom's own probe and its selectionchange, both
 * from the first createRoot, and happy-dom's resize listener behind
 * matchMedia -- are made outside this call and are not in the table.
 */
import { afterEach, describe, expect, it } from 'vitest'

import { installGlobalListeners } from './globalListeners'
import { _resetForTests as resetLayers } from './portals'
import * as selection from './selection'
import * as tip from './tooltip'
import * as updates from '../app/updates'
import * as contextMenu from './contextMenu'
import * as shellWindow from './shellWindow'
import * as browser from '../features/browser/store'
import * as composer from '../features/composer/mount'
import * as boot from '../app/boot'
import * as chips from './proseChips'
import * as menu from './menu'
import * as panes from '../chrome/behaviour/panes'
import * as scrollbars from '../chrome/behaviour/scrollbars'

/* One row per listener: target, event, phase, the handler itself where another
   module owns it, and who it belongs to in words.
   The handler is in the row rather than in a second table keyed by position:
   the old one held `[1, browser.trap] ... [21, updates.onVisible]`, so
   inserting a listener renumbered every row below it and the two tables could
   disagree about which row was which. Four handlers are this module's own and
   cannot be named from outside -- the popover arbitration, the code-block copy,
   the Escape chain and the settings shortcut -- and those four are what the
   order pins instead, together with the source-text case in
   state/escapeOrder.test.ts that holds every keydown the page installs to the
   bubble phase.

   Identity, not shape, because two rows of the same shape are the pairs that
   matter most: which of the two capture-phase pointerdown listeners closes
   first, and which of the two capture-phase scroll listeners reads first, are
   decisions no (target, type, phase) triple can tell apart. */
const ORDER: ReadonlyArray<readonly [string, string, string, unknown, string]> = [
  ['document', 'click', 'capture', browser.trap, 'the link trap (features/browser)'],
  ['window', 'beforeunload', 'bubble', composer.parkDraftNow, 'the unparked draft (features/composer)'],
  ['window', 'resize', 'bubble', composer.fitField, "the field's height cap (features/composer)"],
  ['document', 'scroll', 'capture', scrollbars.onScroll, 'the overlay scrollbars (chrome/behaviour/scrollbars)'],
  ['window', 'resize', 'bubble', scrollbars.onResize, 'the same, dropping every bar (chrome/behaviour/scrollbars)'],
  ['window', 'resize', 'bubble', panes.onResize, 'the two panes re-clamping (chrome/behaviour/panes)'],
  ['document', 'click', 'bubble', chips.onClick, 'a prose chip (state/proseChips)'],
  ['document', 'keydown', 'bubble', chips.onKey, 'a prose chip by keyboard (state/proseChips)'],
  ['document', 'pointerdown', 'capture', menu.onPointerDown, 'a pointer outside the menu (state/menu)'],
  ['document', 'contextmenu', 'bubble', contextMenu.onContextMenu, 'the right-click rule (state/contextMenu)'],
  ['document', 'pointerdown', 'capture', null, 'a pointer outside the two popovers'],
  ['document', 'pointerover', 'bubble', tip.follow, 'the hover pill following (state/tooltip)'],
  ['document', 'scroll', 'capture', tip.hide, 'the hover pill going down (state/tooltip)'],
  ['document', 'mousedown', 'bubble', shellWindow.onMouseDown, "the shell window's drag band"],
  ['document', 'dblclick', 'bubble', shellWindow.onDblClick, "the shell window's zoom"],
  ['document', 'selectionchange', 'bubble', selection.clamp, 'the selection clamp (state/selection)'],
  ['document', 'click', 'bubble', null, "a code block's copy button"],
  ['document', 'keydown', 'bubble', null, 'the Escape order and its three shortcuts'],
  ['document', 'keydown', 'bubble', null, 'the settings shortcut'],
  ['window', 'load', 'bubble', boot.onLoad, 'the boot\'s load handler (app/boot)'],
  ['document', 'visibilitychange', 'bubble', updates.onVisible, 'the build watch (app/updates)'],
] as const

type Row = [string, string, string, boolean, unknown]

/* Every call, in order, each with whether the hover pill's layer was already
   standing when it was made: the layer is raised in the middle of the sequence,
   and a layer raised after the listener that places the pill would place it
   into nothing. */
function record(): Row[] {
  const rows: Row[] = []
  const real = { document: document.addEventListener, window: window.addEventListener }
  const patch = (name: 'document' | 'window') =>
    (type: string, fn: unknown, opts?: unknown): void => {
      const capture = opts === true
        || (typeof opts === 'object' && opts !== null && (opts as AddEventListenerOptions).capture === true)
      rows.push([name, type, capture ? 'capture' : 'bubble', !!document.querySelector('.tipp'), fn])
    }
  document.addEventListener = patch('document') as typeof document.addEventListener
  window.addEventListener = patch('window') as typeof window.addEventListener
  try {
    installGlobalListeners()
  } finally {
    document.addEventListener = real.document
    window.addEventListener = real.window
  }
  return rows
}

afterEach(() => {
  tip._resetForTests()
  resetLayers()
  document.body.innerHTML = ''
})

describe('the page\'s document and window listeners', () => {
  it('registers twenty-one, in one order, nothing twice', () => {
    const rows = record()
    expect(rows.map(([target, type, phase]) => [target, type, phase]))
      .toEqual(ORDER.map(([target, type, phase]) => [target, type, phase]))
  })

  it('gives each row the handler the table says it is', () => {
    const rows = record()
    const named = ORDER.map(([, , , fn], at) => [at, fn]).filter(([, fn]) => fn !== null)
    expect(named.map(([at]) => [at, rows[at as number]?.[4] ?? 'no row there']))
      .toEqual(named)
  })

  it('raises the hover pill\'s layer before the listener that fills it', () => {
    const rows = record()
    const raised = rows.findIndex(([, , , tipUp]) => tipUp)
    const pillFollows = rows.findIndex(([, type]) => type === 'pointerover')
    expect(raised).toBe(pillFollows)
    expect(document.querySelector('.tipp')).toBeTruthy()
    /* And the layer is the body's own child, where the order of everything
       standing at the body is declared (state/portals.ts). */
    expect(document.querySelector('.tipp')!.parentElement).toBe(document.body)
  })

  /* Five, and where they are is the table's: a capture-phase listener runs
     before the element the reader clicked ever sees the event, so which of
     them the page installs and in what order is the contract. */
  it('puts the capture-phase listeners where the table has them', () => {
    const shape = (rows: ReadonlyArray<readonly [string, string, string, ...unknown[]]>): string[] =>
      rows
        .map(([target, type, phase], at) => (phase === 'capture' ? `${at + 1} ${target} ${type}` : null))
        .filter((row) => row !== null)
    expect(shape(record())).toEqual(shape(ORDER))
    expect(shape(ORDER)).toHaveLength(5)
  })
})
