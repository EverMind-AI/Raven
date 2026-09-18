// @vitest-environment happy-dom
/* The order Escape closes things in.
 *
 * The order was a fourteen-branch if chain in the page's chrome, and this file
 * asserted it against that chain's source text, so that stage C11 could change
 * what is asserted without
 * touching the expectation. C11 has: the order is the ordered table in
 * src/state/escapeOrder.ts -- a table, not a stack, because each entry answers
 * "am I open" when Escape arrives, so "the last one opened closes first" never
 * happens, which is what the chain did too.
 *
 * The array below is that expectation, unchanged. What is asserted against it
 * is now the table, every entry's own predicate and action against a fixture
 * page, and all ninety-one pairs of layers. The three capture-phase handlers
 * each open sheet registers run *before* the table and two of them act on
 * Escape without stopping propagation, so one Escape can both deny an approval
 * and interrupt the running turn: that is pinned here with the real sheet.
 */
// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { openApproval } from '../features/composer/approve'
import * as turn from '../features/composer/turn'
import * as connections from '../features/connections/store'
import * as cron from '../features/cron/store'
import * as extAgents from '../features/extAgents/store'
import * as memory from '../features/memory/store'
import * as playbooks from '../features/playbooks/store'
import { resetTranslator, setTranslator } from '../i18n/t'
import { _resetForTests as sessionReset, setCurrent } from '../lib/session'
import * as escapeOrder from './escapeOrder'
import * as find from './find'
import { installEscapeOrder } from './globalListeners'
import * as settingsDialog from './settings'
import * as sheets from './sheetRack'
import { resetSources, sources } from './sources'

import type { ComposerSource } from '../features/composer/types'

/* The fourteen, in the order Escape reaches them. Each item is the text the
   chain tests to decide whether that layer is on screen -- a selector for the
   twelve elements, the predicate's own name for the last two, which have no
   element of their own to look at. */
const LAYER_IDS = [
  '.lightbox',
  '#veil',
  '#connVeil',
  '#detail',
  '#jobVeil',
  '#cronPage',
  '#memoryPage',
  '#playbooksPage',
  '#extAgentsPage',
  '#connectionsPage',
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
  '<section class="page" id="extAgentsPage" data-open="false"></section>',
  '<section class="page" id="connectionsPage" data-open="false"></section>',
  '<section class="page" id="memoryPage" data-open="false"></section>',
  '<section class="page" id="playbooksPage" data-open="false"></section>',
  '<section class="page" id="cronPage" data-open="false"></section>',
  '<div class="veil" id="jobVeil" data-open="false"><button id="jobNo"></button></div>',
  '<aside class="detail" id="detail" data-open="false"><div class="body" id="dBody"></div></aside>',
  '<div class="veil setveil" id="setVeil" data-open="false"><div id="setModal"></div></div>',
  '<div class="veil" id="veil" data-open="false"><button id="cfNo"></button></div>',
  '<div class="veil" id="connVeil" data-open="false"></div>',
  '</div>',
  '<button id="railShow"></button>',
].join('')

/* What each layer's close lands on. Every island verb the table reaches is one
   export of one store, stood in for one name at a time -- so the rest of that
   store answers as it really does, and a close that reaches past its own verb
   is visible. */
const spies = {
  connDialog: vi.fn(),
  connClose: vi.fn(),
  cronClose: vi.fn(),
  memClose: vi.fn(),
  pbClose: vi.fn(),
  extAgentsClose: vi.fn(),
  stop: vi.fn(),
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
  '#memoryPage': { up: flag('memoryPage'), taken: called(spies.memClose) },
  '#playbooksPage': { up: flag('playbooksPage'), taken: called(spies.pbClose) },
  /* The capabilities page closes through the page store, which lowers all
     seven sections -- so its own mark is the one section it was asked about. */
  '#extAgentsPage': { up: flag('extAgentsPage'), taken: called(spies.extAgentsClose) },
  '#connectionsPage': { up: flag('connectionsPage'), taken: called(spies.connClose) },
  'setIsOpen()': { up: () => settingsDialog.open(), taken: () => !settingsDialog.isOpen() },
  'turn.busy()': { up: () => turn.dispatch({ type: 'send' }), taken: called(spies.stop) },
}

setTranslator((key) => key)

beforeAll(() => {
  /* Once for the file: the listener is the document's, and rebuilding the body
     between cases does not take it off. */
  installEscapeOrder()
})

beforeEach(() => {
  document.body.innerHTML = PAGE
  cancelled = []
  document.getElementById('cfNo')!.onclick = () => { cancelled.push('cfNo') }
  document.getElementById('jobNo')!.onclick = () => { cancelled.push('jobNo') }
  for (const spy of Object.values(spies)) spy.mockClear()
  /* Each layer's own close, stood in for one export at a time: what is under
     test is which one the key reaches, not what any of them does. */
  vi.spyOn(connections, 'closeDialog').mockImplementation(spies.connDialog)
  vi.spyOn(connections, 'close').mockImplementation(spies.connClose)
  vi.spyOn(cron, 'close').mockImplementation(spies.cronClose)
  vi.spyOn(memory, 'close').mockImplementation(spies.memClose)
  vi.spyOn(playbooks, 'closePage').mockImplementation(spies.pbClose)
  vi.spyOn(extAgents, 'close').mockImplementation(spies.extAgentsClose)
  sources.composer = { stop: spies.stop } as unknown as ComposerSource
  settingsDialog.close()
  /* The row's flag outlives a case now that it is a store's rather than the
     box's `hidden` -- the same zeroing find.test.ts's harness needed. */
  find.toggle(false)
  turn._resetForTests()
  sessionReset()
  setCurrent('a')
  sheets._resetForTests()
})

afterEach(() => {
  vi.restoreAllMocks()
  resetSources()
  resetTranslator()
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
    expect(escapeOrder.ESCAPE_ORDER.map((layer) => layer.id)).toEqual([...LAYER_IDS])
  })

  it('has no fifteenth entry, and every entry is in the fixture', () => {
    expect(escapeOrder.ESCAPE_ORDER).toHaveLength(LAYER_IDS.length)
    expect(Object.keys(LAYERS)).toEqual([...LAYER_IDS])
  })

  it('reads nothing as open on a page where nothing is', () => {
    expect(escapeOrder.ESCAPE_ORDER.filter((layer) => layer.isOpen())).toEqual([])
    expect(escapeOrder.dispatch()).toBe(false)
  })

  it.each([...LAYER_IDS])('sees %s open and takes it back', (id) => {
    LAYERS[id]!.up()
    expect(escapeOrder.ESCAPE_ORDER.find((layer) => layer.id === id)!.isOpen()).toBe(true)
    expect(escapeOrder.dispatch()).toBe(true)
    expect(LAYERS[id]!.taken()).toBe(true)
  })

  /* The table's whole point: with two layers up, which one goes is the table's
     order and not the order they were raised in. A stack would answer the
     second of each pair. */
  const pairs = LAYER_IDS.flatMap((first, i) =>
    LAYER_IDS.slice(i + 1).map((second) => ({ first, second })))

  it('has ninety-one pairs to answer for', () => {
    expect(pairs).toHaveLength(66)
  })

  it.each(pairs)('takes back $first and leaves $second alone', ({ first, second }) => {
    LAYERS[second]!.up()
    LAYERS[first]!.up()
    expect(escapeOrder.dispatch()).toBe(true)
    expect(LAYERS[first]!.taken()).toBe(true)
    expect(LAYERS[second]!.taken()).toBe(false)
  })
})

describe('the one listener that reads the order', () => {
  it('takes back the first open layer on Escape', () => {
    LAYERS['#memoryPage']!.up()
    key('Escape')
    expect(spies.memClose).toHaveBeenCalledTimes(1)
  })

  /* Escape ends a composition; it must not also close a panel behind the
     reader's back. Both spellings, because older input methods send the
     keyCode instead of the flag. */
  it('leaves an input method alone mid-composition', () => {
    LAYERS['#memoryPage']!.up()
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

  it('is a bubble-phase listener, after the three the sheets register', () => {
    /* Which is what lets one Escape do two things. The count is the pinned
       fact: two in the approval sheet, one in the clarify sheet, all three
       registered with capture. */
    const capture = /document\.addEventListener\('keydown', onKey, true\)/g
    expect(source('src/features/composer/approve.ts').match(capture) ?? []).toHaveLength(2)
    expect(source('src/features/composer/clarify.ts').match(capture) ?? []).toHaveLength(1)
    /* And every keydown the page installs is bubble-phase, this one included:
       a third argument would show up in one of the matches below. Sorted,
       because what the three are registered in is declared in one place and
       asserted by the case above, not by where they sit in this text. */
    const listener = source('src/state/globalListeners.ts')
    expect(listener).toMatch(/document\.addEventListener\('keydown', onEscapeOrder\)\n/)
    expect([...listener.matchAll(/addEventListener\('keydown'([^)]*)\)/g)].map((m) => m[1]).sort())
      .toEqual([', chipKey', ', onEscapeOrder', ', onSettingsKey'])
  })

  /* And bubbling is what lets a field keep the key: the search row stops
     Escape on its own input so that dismissing the row does not also take a
     page down behind it (state/find.ts). A capture-phase listener would have
     read the key before the field ever saw it. */
  it('does not see a key an element stopped', () => {
    find.install()
    find.toggle(true)
    LAYERS['#memoryPage']!.up()
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
