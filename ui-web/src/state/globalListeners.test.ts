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

/* Target, event, phase, and who the handler belongs to. The fourth column is
   not asserted -- nothing about a listener says whose it is -- and is here
   because a bare list of twenty-one triples is unreadable. */
const ORDER = [
  ['document', 'click', 'capture', 'the link trap (features/browser)'],
  ['window', 'beforeunload', 'bubble', 'the unparked draft (features/composer)'],
  ['window', 'resize', 'bubble', "the field's height cap (features/composer)"],
  ['document', 'scroll', 'capture', 'the overlay scrollbars (chrome/behaviour/scrollbars)'],
  ['window', 'resize', 'bubble', 'the same, dropping every bar (chrome/behaviour/scrollbars)'],
  ['window', 'resize', 'bubble', 'the two panes re-clamping (chrome/behaviour/panes)'],
  ['document', 'click', 'bubble', 'a prose chip (state/proseChips)'],
  ['document', 'keydown', 'bubble', 'a prose chip by keyboard (state/proseChips)'],
  ['document', 'pointerdown', 'capture', 'a pointer outside the menu (state/menu)'],
  ['document', 'contextmenu', 'bubble', 'the right-click rule (state/contextMenu)'],
  ['document', 'pointerdown', 'capture', 'a pointer outside the two popovers'],
  ['document', 'pointerover', 'bubble', 'the hover pill following (state/tooltip)'],
  ['document', 'scroll', 'capture', 'the hover pill going down (state/tooltip)'],
  ['document', 'mousedown', 'bubble', "the shell window's drag band"],
  ['document', 'dblclick', 'bubble', "the shell window's zoom"],
  ['document', 'selectionchange', 'bubble', 'the selection clamp (state/selection)'],
  ['document', 'click', 'bubble', "a code block's copy button"],
  ['document', 'keydown', 'bubble', 'the Escape order and its three shortcuts'],
  ['document', 'keydown', 'bubble', 'the settings shortcut'],
  ['window', 'load', 'bubble', 'the boot\'s load handler (app/boot)'],
  ['document', 'visibilitychange', 'bubble', 'the build watch (app/updates)'],
] as const

type Row = [string, string, string, boolean, unknown]

/* Which handler each row is, for every row whose handler another module owns.
   Four are this module's own and cannot be named from outside: the popover
   arbitration, the code-block copy, the Escape chain and the settings shortcut
   -- and those four are exactly what the two source-text cases in
   state/overlays.test.ts and the order above pin instead.

   Identity, not shape, because two rows of the same shape are the pairs that
   matter most: which of the two capture-phase pointerdown listeners closes
   first, and which of the two capture-phase scroll listeners reads first, are
   decisions no (target, type, phase) triple can tell apart. */
const HANDLERS: readonly [number, unknown][] = [
  [1, browser.trap],
  [2, composer.parkDraftNow],
  [3, composer.fitField],
  [4, scrollbars.onScroll],
  [5, scrollbars.onResize],
  [6, panes.onResize],
  [7, chips.onClick],
  [8, chips.onKey],
  [9, menu.onPointerDown],
  [10, contextMenu.onContextMenu],
  [12, tip.follow],
  [13, tip.hide],
  [14, shellWindow.onMouseDown],
  [15, shellWindow.onDblClick],
  [16, selection.clamp],
  [20, boot.onLoad],
  [21, updates.onVisible],
]

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
    expect(HANDLERS.map(([at]) => [at, rows[at - 1]?.[4] ?? 'no row there']))
      .toEqual(HANDLERS.map(([at, fn]) => [at, fn]))
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

  it('puts the five capture-phase listeners where the page has them', () => {
    const capture = record()
      .map(([target, type, phase], at) => (phase === 'capture' ? `${at + 1} ${target} ${type}` : null))
      .filter((row) => row !== null)
    expect(capture).toEqual([
      '1 document click',
      '4 document scroll',
      '9 document pointerdown',
      '11 document pointerdown',
      '13 document scroll',
    ])
  })
})
