// @vitest-environment happy-dom
/* The order Escape closes things in.
 *
 * The order was a fourteen-branch if chain in the legacy chrome
 * (src/legacy/demo/150-chrome.js) and this file asserted it against that
 * chain's source text, so that stage C11 could change what is asserted without
 * touching the expectation. C11 has: the order is the ordered table in
 * src/state/overlays.ts -- a table, not a stack, because each entry answers "am
 * I open" when Escape arrives, so "the last one opened closes first" never
 * happens, which is what the chain did too.
 *
 * The array below is that expectation, unchanged. What is asserted against it
 * is now the table, every entry's own predicate and action against a fixture
 * page, and all ninety-one pairs of layers. The three capture-phase handlers
 * each open sheet registers run *before* the table and two of them act on
 * Escape without stopping propagation, so one Escape can both deny an approval
 * and interrupt the running turn: that is pinned here with the real sheet.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'

import { openApproval } from '../features/composer/approve'
import * as sheets from '../features/composer/sheets'
import * as turn from '../features/composer/turn'
import { islands } from '../islands'
import { resetShell, setShell } from '../shell/bridge'
import * as find from '../shell/find'
import { _resetForTests as sessionReset, setCurrent } from '../shell/session'
import { installEscapeChain } from './globalListeners'
import * as overlays from './overlays'
import * as settingsDialog from './settingsDialog'
import { resetSources, sources } from './sources'

import type { ComposerSource } from '../features/composer/types'
import type { Shell } from '../shell/bridge'

/* The fourteen, in the order Escape reaches them. Each item is the text the
   chain tests to decide whether that layer is on screen -- a selector for the
   twelve elements, the predicate's own name for the last two, which have no
   element of their own to look at. */
const ESCAPE_ORDER = [
  '.lightbox',
  '#veil',
  '#connVeil',
  '#detail',
  '#jobVeil',
  '#cronPage',
  '#memPage',
  '#pbPage',
  '#kbPage',
  '#capsPage',
  '#xaPage',
  '#connPage',
  'setIsOpen()',
  'turn.busy()',
] as const

const source = (path: string): string => readFileSync(path, 'utf8') as string

/* Every element the table reads or clicks, in a page shaped like the real one:
   the twelve carriers of data-open, the two cancel buttons the two sheets are
   answered through, the new-task button Cmd+N clicks, and the rail's three
   pieces the two rail shortcuts write. */
const PAGE = [
  '<div class="app" data-rail="on">',
  '<button id="newBtn"></button>',
  '<div class="find" id="findBox" hidden><input id="sfind"></div>',
  '<div class="chat"><div class="dock"><div class="sheets" id="sheetRack"></div>',
  '<div class="dock-in"><textarea id="ta"></textarea></div></div></div>',
  '<section class="page" id="capsPage" data-open="false"></section>',
  '<section class="page" id="xaPage" data-open="false"></section>',
  '<section class="page" id="connPage" data-open="false"></section>',
  '<section class="page" id="memPage" data-open="false"></section>',
  '<section class="page" id="pbPage" data-open="false"></section>',
  '<section class="page" id="kbPage" data-open="false"></section>',
  '<section class="page" id="cronPage" data-open="false"></section>',
  '<div class="veil" id="jobVeil" data-open="false"><button id="jobNo"></button></div>',
  '<aside class="detail" id="detail" data-open="false"><div class="body" id="dBody"></div></aside>',
  '<div class="veil setveil" id="setVeil" data-open="false"><div id="setModal"></div></div>',
  '<div class="veil" id="veil" data-open="false"><button id="cfNo"></button></div>',
  '<div class="veil" id="connVeil" data-open="false"></div>',
  '</div>',
  '<button id="railShow"></button>',
].join('')

/* What each layer's close lands on. Every island verb the table reaches is a
   member of the island bag, which is an object, so a stand-in is an
   assignment rather than a mocked module -- and this file is inside the import
   cycle the legacy chrome sits in, where a mocked module would be a second
   copy of half the page. Two of these are not probes but quiet stand-ins for
   verbs a close reaches through: the cron sheet and the rail's marks, which the
   page store and the settings dialog call on their way out. */
const spies = {
  connDialog: vi.fn(),
  connClose: vi.fn(),
  cronClose: vi.fn(),
  cronSheet: vi.fn(),
  memClose: vi.fn(),
  pbClose: vi.fn(),
  kbClose: vi.fn(),
  xaClose: vi.fn(),
  railMark: vi.fn(),
  stop: vi.fn(),
}
const real = {
  connDialog: islands.connections.closeDialog,
  connClose: islands.connections.close,
  cronClose: islands.cron.close,
  cronSheet: islands.cron.closeSheet,
  memClose: islands.memory.close,
  pbClose: islands.playbooks.close,
  kbClose: islands.knowledge.close,
  xaClose: islands.xa.close,
  railMark: islands.rail.markNew,
}

/* Which button was pressed on the two sheets that are answered by a click. */
let cancelled: string[] = []

const flag = (id: string) => (): void => { document.getElementById(id)!.dataset.open = 'true' }
const lowered = (id: string) => (): boolean => document.getElementById(id)!.dataset.open === 'false'
const called = (spy: { mock: { calls: unknown[] } }) => (): boolean => spy.mock.calls.length > 0

/* One entry per layer: how it gets on screen, and how to tell its own close
   ran -- the effect of that close and nothing wider, so that "the layer below
   was left alone" means the layer below, not a flag some other close also
   lowers. */
const LAYERS: Record<string, { up: () => void; taken: () => boolean }> = {
  '.lightbox': {
    up: () => {
      const box = document.createElement('button')
      box.className = 'lightbox'
      document.body.appendChild(box)
    },
    taken: () => !document.querySelector('.lightbox'),
  },
  '#veil': { up: flag('veil'), taken: () => cancelled.includes('cfNo') },
  '#connVeil': { up: flag('connVeil'), taken: called(spies.connDialog) },
  '#detail': { up: flag('detail'), taken: lowered('detail') },
  '#jobVeil': { up: flag('jobVeil'), taken: () => cancelled.includes('jobNo') },
  '#cronPage': { up: flag('cronPage'), taken: called(spies.cronClose) },
  '#memPage': { up: flag('memPage'), taken: called(spies.memClose) },
  '#pbPage': { up: flag('pbPage'), taken: called(spies.pbClose) },
  '#kbPage': { up: flag('kbPage'), taken: called(spies.kbClose) },
  /* The capabilities page closes through the page store, which lowers all
     seven sections -- so its own mark is the one section it was asked about. */
  '#capsPage': { up: flag('capsPage'), taken: lowered('capsPage') },
  '#xaPage': { up: flag('xaPage'), taken: called(spies.xaClose) },
  '#connPage': { up: flag('connPage'), taken: called(spies.connClose) },
  'setIsOpen()': { up: () => settingsDialog.open(), taken: () => !settingsDialog.isOpen() },
  'turn.busy()': { up: () => turn.dispatch({ type: 'send' }), taken: called(spies.stop) },
}

const shell: Shell = { T: (key) => key, confirmAsk: () => {}, showPage: () => {} }

beforeAll(() => {
  /* Once for the file: the listener is the document's, and rebuilding the body
     between cases does not take it off. */
  installEscapeChain()
})

beforeEach(() => {
  document.body.innerHTML = PAGE
  cancelled = []
  document.getElementById('cfNo')!.onclick = () => { cancelled.push('cfNo') }
  document.getElementById('jobNo')!.onclick = () => { cancelled.push('jobNo') }
  for (const spy of Object.values(spies)) spy.mockClear()
  islands.connections.closeDialog = spies.connDialog
  islands.connections.close = spies.connClose
  islands.cron.close = spies.cronClose
  islands.cron.closeSheet = spies.cronSheet
  islands.memory.close = spies.memClose
  islands.playbooks.close = spies.pbClose
  islands.knowledge.close = spies.kbClose
  islands.xa.close = spies.xaClose
  islands.rail.markNew = spies.railMark
  sources.composer = { stop: spies.stop } as unknown as ComposerSource
  settingsDialog.close()
  /* The row's flag outlives a case now that it is a store's rather than the
     box's `hidden` -- the same zeroing find.test.ts's harness needed. */
  find.toggle(false)
  turn._resetForTests()
  sessionReset()
  setCurrent('a')
  sheets._resetForTests()
  setShell(shell)
})

afterEach(() => {
  Object.assign(islands.connections, { closeDialog: real.connDialog, close: real.connClose })
  Object.assign(islands.cron, { close: real.cronClose, closeSheet: real.cronSheet })
  islands.memory.close = real.memClose
  islands.playbooks.close = real.pbClose
  islands.knowledge.close = real.kbClose
  islands.xa.close = real.xaClose
  islands.rail.markNew = real.railMark
  resetSources()
  resetShell()
  sessionReset()
  document.body.innerHTML = ''
})

afterAll(() => {
  turn._resetForTests()
})

const key = (k: string, over: Partial<KeyboardEventInit> = {}): KeyboardEvent => {
  const e = new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true, ...over })
  document.dispatchEvent(e)
  return e
}

describe('the Escape priority order', () => {
  it('is the order the table reaches the fourteen layers in', () => {
    expect(overlays.ORDER.map((layer) => layer.id)).toEqual([...ESCAPE_ORDER])
  })

  it('has no fifteenth entry, and every entry is in the fixture', () => {
    expect(overlays.ORDER).toHaveLength(ESCAPE_ORDER.length)
    expect(Object.keys(LAYERS)).toEqual([...ESCAPE_ORDER])
  })

  it('reads nothing as open on a page where nothing is', () => {
    expect(overlays.ORDER.filter((layer) => layer.isOpen())).toEqual([])
    expect(overlays.dispatch()).toBe(false)
  })

  it.each([...ESCAPE_ORDER])('sees %s open and takes it back', (id) => {
    LAYERS[id]!.up()
    expect(overlays.ORDER.find((layer) => layer.id === id)!.isOpen()).toBe(true)
    expect(overlays.dispatch()).toBe(true)
    expect(LAYERS[id]!.taken()).toBe(true)
  })

  /* The table's whole point: with two layers up, which one goes is the table's
     order and not the order they were raised in. A stack would answer the
     second of each pair. */
  const pairs = ESCAPE_ORDER.flatMap((first, i) =>
    ESCAPE_ORDER.slice(i + 1).map((second) => ({ first, second })))

  it('has ninety-one pairs to answer for', () => {
    expect(pairs).toHaveLength(91)
  })

  it.each(pairs)('takes back $first and leaves $second alone', ({ first, second }) => {
    LAYERS[second]!.up()
    LAYERS[first]!.up()
    expect(overlays.dispatch()).toBe(true)
    expect(LAYERS[first]!.taken()).toBe(true)
    expect(LAYERS[second]!.taken()).toBe(false)
  })
})

describe('the one listener that reads the order', () => {
  it('takes back the first open layer on Escape', () => {
    LAYERS['#memPage']!.up()
    key('Escape')
    expect(spies.memClose).toHaveBeenCalledTimes(1)
  })

  /* Escape ends a composition; it must not also close a panel behind the
     reader's back. Both spellings, because older input methods send the
     keyCode instead of the flag. */
  it('leaves an input method alone mid-composition', () => {
    LAYERS['#memPage']!.up()
    key('Escape', { isComposing: true })
    key('Escape', { keyCode: 229 })
    expect(spies.memClose).not.toHaveBeenCalled()
    key('Escape')
    expect(spies.memClose).toHaveBeenCalledTimes(1)
  })

  it('does nothing visible when nothing is open', () => {
    const e = key('Escape')
    expect(e.defaultPrevented).toBe(false)
    expect(spies.stop).not.toHaveBeenCalled()
  })

  /* The row's `hidden` is <FindRow/>'s to render off this flag (C5), so the
     flag is what the shortcut is read by; the rail's is written by hand. */
  it('shows the rail and the search row on Cmd+F', () => {
    const e = key('f', { metaKey: true })
    expect(e.defaultPrevented).toBe(true)
    expect(document.documentElement.dataset.rail).toBe('on')
    expect(find.get().open).toBe(true)
  })

  it('folds and unfolds the rail on Cmd+backslash', () => {
    key('\\', { metaKey: true })
    expect(document.querySelector<HTMLElement>('.app')!.dataset.rail).toBe('off')
    const e = key('\\', { metaKey: true })
    expect(e.defaultPrevented).toBe(true)
    expect(document.querySelector<HTMLElement>('.app')!.dataset.rail).toBe('on')
  })

  it('starts a new task on Cmd+N, but not from inside a field', () => {
    const clicks: string[] = []
    document.getElementById('newBtn')!.onclick = () => { clicks.push('newBtn') }
    const e = key('n', { metaKey: true })
    expect(e.defaultPrevented).toBe(true)
    expect(clicks).toEqual(['newBtn'])
    document.getElementById('ta')!.focus()
    const inside = key('n', { metaKey: true })
    expect(inside.defaultPrevented).toBe(false)
    expect(clicks).toEqual(['newBtn'])
  })

  /* Where it is installed from is the contract: document listeners are
     registered in one order at boot, and this one sits between the chrome's
     code-block click and its settings shortcut, which is where the handler it
     replaces was added. */
  it('is installed by the chrome from where the old handler was added', () => {
    const chrome = source('src/legacy/demo/150-chrome.js')
    const copy = chrome.indexOf("document.addEventListener('click'")
    const chain = chrome.indexOf('installEscapeChain()')
    const settings = chrome.indexOf("document.addEventListener('keydown'")
    expect(copy, 'the chrome no longer catches the code-block click').toBeGreaterThan(-1)
    expect(chain, 'the chrome no longer installs the Escape order').toBeGreaterThan(copy)
    expect(settings, 'the settings shortcut is no longer after it').toBeGreaterThan(chain)
    expect(chrome.match(/document\.addEventListener\('keydown'/g) ?? []).toHaveLength(1)
  })

  it('is a bubble-phase listener, after the three the sheets register', () => {
    /* Which is what lets one Escape do two things. The count is the pinned
       fact: two in the approval sheet, one in the clarify sheet, all three
       registered with capture. */
    const capture = /document\.addEventListener\('keydown', onKey, true\)/g
    expect(source('src/features/composer/approve.ts').match(capture) ?? []).toHaveLength(2)
    expect(source('src/features/composer/clarify.ts').match(capture) ?? []).toHaveLength(1)
    const listener = source('src/state/globalListeners.ts')
    expect(listener).toMatch(/document\.addEventListener\('keydown', \(e\) => \{/)
    expect(listener.match(/addEventListener\(/g) ?? []).toHaveLength(1)
    expect(listener).not.toMatch(/, true\)/)
    expect(listener).not.toMatch(/capture/)
  })

  /* And bubbling is what lets a field keep the key: the search row stops
     Escape on its own input so that dismissing the row does not also take a
     page down behind it (shell/find.ts). A capture-phase listener would have
     read the key before the field ever saw it. */
  it('does not see a key an element stopped', () => {
    find.install()
    find.toggle(true)
    LAYERS['#memPage']!.up()
    document.getElementById('sfind')!
      .dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    expect(find.get().open).toBe(false)
    expect(spies.memClose).not.toHaveBeenCalled()
  })

  /* The approval sheet's capture handler denies and does not stop the event,
     so the same Escape carries on into the table and ends at the last entry --
     which interrupts the turn the approval was blocking. */
  it('both denies an approval and stops the running turn', () => {
    const said: string[] = []
    openApproval({ approvalId: '1', command: 'rm -rf build/', description: 'shell' },
      (choice) => said.push(choice), 'a')
    turn.dispatch({ type: 'send' })
    key('Escape')
    expect(said).toEqual(['deny'])
    expect(spies.stop).toHaveBeenCalledTimes(1)
  })
})
