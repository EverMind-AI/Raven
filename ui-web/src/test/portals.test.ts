// @vitest-environment happy-dom
/* Everything that sits at the body rather than inside a page, and the order it
 * sits in.
 *
 * Two steps of the `--z` ladder in src/styles/page.css are deliberate ties --
 * `--z-shade` with `--z-tip` at 90, and `--z-picker` with the inline 46 the two
 * composer popovers set -- so for those four elements the DOM order at the body
 * is the whole of the stacking decision. The order is declared in
 * src/state/portals.ts, which is also what hands out the four standing layers,
 * and if that table says one thing while the page does another the ties flip
 * with no test and no pixel to catch it. This file reads the table against the
 * page: the boot goldens, the markup, the two popovers that reparent
 * themselves, and the body while every overlay is up. The table's own accessor
 * has its own test beside it (src/state/portals.test.ts).
 */
import { describe, expect, it, vi } from 'vitest'

// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'

import * as menu from '../shell/menu'
import * as session from '../shell/session'
import * as tier from '../shell/tier'
import * as toast from '../shell/toast'
import { resetShell, setShell } from '../shell/bridge'
import * as confirm from '../state/confirm'
import { BOOT_BODY_ORDER, LAYERS, PORTALS, _resetForTests as resetLayers, host } from '../state/portals'
import { resetSources, setSources } from '../state/sources'
import { bodySiblings } from './domSnapshot'
import { mountPageRoot } from './pageRoot'

import type { Shell } from '../shell/bridge'
import type { TierReply, TierSource } from '../shell/tier'

const source = (path: string): string => readFileSync(path, 'utf8') as string

/* The key BOOT_BODY_ORDER files an element under: its id, or its classes when
   it has none. Read from a signature line, so a golden's line and a live
   element's `bodySiblings` entry are reduced the same way. */
function keyOf(line: string): string {
  const parsed = /^([a-z]+)(?:#([\w-]+))?((?:\.[^.[]+)*)/.exec(line)
  expect(parsed, `unreadable body-level line: ${line}`).toBeTruthy()
  const [, tag = '', id, classes = ''] = parsed!
  if (id) return `${tag}#${id}`
  return `${tag}${classes}`
}

/* The body-level lines of a boot golden: the golden's own bytes are already
   pinned by scripts/boot-snapshot.mjs, so what is read here is only the order. */
function goldenBodyKeys(path: string): string[] {
  return source(path)
    .split('\n')
    .filter((line) => line.length > 0 && !line.startsWith(' '))
    .map(keyOf)
}

/** The body as it stands, keyed the way the goldens key it. */
const bodyKeys = (doc: Document): string[] => bodySiblings(doc).map(keyOf)

/* page.html's two static hosts, and the band the composer dock renders into --
   which is where the two popovers start, so a case that reads them mounts the
   page root over this (src/chrome/Dock.tsx). */
function pageMarkup(): Document {
  const html = source('src/page.html')
  const body = html
    .slice(html.indexOf('<body>') + '<body>'.length, html.indexOf('</body>'))
    .replace(/<script[\s\S]*?<\/script>/g, '')
  document.body.innerHTML = body
  return document
}

describe('the portal table', () => {
  it('lists thirteen hosts and no two of them twice', () => {
    expect(PORTALS).toHaveLength(13)
    expect(new Set(PORTALS.map((p) => p.selector)).size).toBe(13)
  })

  it('places the two static hosts at the body in the markup itself', () => {
    const doc = pageMarkup()
    for (const id of ['menu', 'toasts']) {
      const el = doc.getElementById(id)
      expect(el, `#${id} is not in page.html`).toBeTruthy()
      expect(el!.parentElement).toBe(doc.body)
    }
  })

  it('agrees with the boot golden on the body order', () => {
    const stub = goldenBodyKeys('scripts/__golden__/boot-stub.txt')
    expect(stub).toEqual([...BOOT_BODY_ORDER])
    /* With no gateway behind the page the failure bar is up, one past the rest,
       which is the only difference between the two modes at this level. */
    const live = goldenBodyKeys('scripts/__golden__/boot-live-noserver.txt')
    expect(live).toEqual([...BOOT_BODY_ORDER, 'div.topfail'])
    for (const portal of PORTALS) {
      if (portal.at === 'last') continue
      expect(stub[portal.at - 1], `${portal.selector} is not body child ${portal.at}`)
        .toBe(portal.bootKey)
    }
  })

  it('gives the tooltip layer a place before the update shade, breaking the tie at 90', () => {
    /* page.css says the tie is deliberate and reads as if a tooltip may sit on
       the shade; the DOM says otherwise, because .tipp is appended while the
       page installs and .upshade only when an upgrade starts. Stage C copies
       what is measured, not what the comment intends. */
    expect(BOOT_BODY_ORDER.indexOf('div.tipp')).toBeGreaterThan(-1)
    expect(PORTALS.find((p) => p.selector === '.tipp')!.at).toBe(21)
    expect(PORTALS.find((p) => p.selector === '.upshade')!.at).toBe('last')
  })

  it('gives the model picker a place before the two popovers, breaking the tie at 46', () => {
    const picker = PORTALS.find((p) => p.selector === 'pickHost')!
    expect(picker.at).toBe(19)
    for (const id of ['#permPop', '#tierPop']) {
      expect(PORTALS.find((p) => p.selector === id)!.at).toBe('last')
    }
  })

  /* The page's own root renders into the containers page.html provides and
     adds nothing beside them: it is detached, and a root AT the body would
     clear the regions instead of joining them (see src/main.tsx). */
  it('leaves the body order untouched when the page root renders', () => {
    const doc = pageMarkup()
    const before = bodySiblings(doc)
    const unmount = mountPageRoot()
    try {
      expect(bodySiblings(doc)).toEqual(before)
    } finally {
      unmount()
    }
  })

  /* Every overlay at once. The menu's rows, the notices and the confirm sheet
     all render into hosts that are already at the body, so raising them may
     neither reorder the body nor add to it -- which is the whole of why they
     are portals into standing hosts rather than roots of their own. */
  it('keeps the boot order while the menu, three notices and the confirm sheet are up', () => {
    vi.useFakeTimers()
    /* One root, as the page has one: a second standing root would render the
       overlays a second time into the same hosts. */
    let unmount = (): void => {}
    try {
      const doc = pageMarkup()
      /* The two pre-JavaScript shells are taken down at boot, which is why the
         golden's static half is seventeen regions rather than nineteen. */
      doc.getElementById('splash')!.remove()
      doc.getElementById('noJs')!.remove()
      resetLayers()
      for (const layer of LAYERS) host(layer)
      unmount = mountPageRoot()
      expect(bodyKeys(doc)).toEqual([...BOOT_BODY_ORDER])
      menu.show(10, 12, [{ label: 'Rename', fn: () => {} }, '-', { label: 'Delete', bad: true, fn: () => {} }])
      toast.show('one')
      toast.show('two')
      toast.show('three', { label: 'Undo', fn: () => {} })
      confirm.ask('Delete this?', 'It cannot be undone.', 'Delete', () => {})
      /* Up, not merely asked for: an overlay that rendered nothing at all would
         pass the order assertion below without ever being on screen. */
      expect(doc.getElementById('menu')!.children).toHaveLength(3)
      expect(doc.getElementById('menu')!.dataset.open).toBe('true')
      expect(doc.getElementById('toasts')!.children).toHaveLength(3)
      expect(doc.getElementById('veil')!.dataset.open).toBe('true')
      expect(bodyKeys(doc)).toEqual([...BOOT_BODY_ORDER])
    } finally {
      unmount()
      vi.useRealTimers()
      resetLayers()
    }
  })

  it('starts the two popovers inside the composer card', () => {
    const doc = pageMarkup()
    const unmount = mountPageRoot()
    try {
      for (const id of ['permPop', 'tierPop']) {
        const el = doc.getElementById(id)
        expect(el, `#${id} is not rendered`).toBeTruthy()
        expect(el!.parentElement!.className).toBe('dock-in')
      }
    } finally {
      unmount()
    }
  })
})

/* The reparenting itself, on the live module rather than on the markup: the
   fixture is tier.test.ts's, which is the band the panel is rendered into
   (src/chrome/TierPop.tsx) -- the composer card comes with it, and the card is
   what the panel has to open clear of. */
describe('a popover that has been opened', () => {
  const MENU = [
    { id: 'medium', name: 'Medium', description: 'The least effort a sub-agent is asked for.' },
    { id: 'high', name: 'High', description: 'The middle amount of effort, between the other two.' },
    { id: 'max', name: 'Max', description: 'The most effort a sub-agent is asked for.' },
  ]

  let unmount = (): void => {}

  async function open(): Promise<HTMLElement> {
    setShell({ T: (key) => key, confirmAsk: () => {}, showPage: () => {} } as Shell)
    document.body.innerHTML = '<div class="dock"></div>'
    tier._resetForTests()
    session._resetForTests()
    session.setCurrent('cli:one')
    const src: TierSource = {
      read: async (): Promise<TierReply> => ({ mode: 'high', availableModes: MENU }),
      set: async (mode): Promise<TierReply> => ({ mode: mode ?? 'high', availableModes: MENU }),
    }
    setSources({ tier: src })
    unmount = mountPageRoot()
    await tier.load()
    tier.open()
    return document.getElementById('tierPop')!
  }

  function reset(): void {
    unmount()
    unmount = () => {}
    resetShell()
    document.body.innerHTML = ''
    tier._resetForTests()
    session._resetForTests()
    resetSources()
  }

  it('hangs off the body, last of its children', async () => {
    try {
      const pop = await open()
      expect(pop.parentElement).toBe(document.body)
      expect(document.body.lastElementChild).toBe(pop)
      expect(bodySiblings()).toContain('div#tierPop.pop[data-open=true]')
    } finally {
      reset()
    }
  })

  it('stays there when it closes -- the move is once, not per open', async () => {
    try {
      const pop = await open()
      tier.close()
      expect(pop.parentElement).toBe(document.body)
      expect(pop.dataset.open).toBe('false')
    } finally {
      reset()
    }
  })
})
