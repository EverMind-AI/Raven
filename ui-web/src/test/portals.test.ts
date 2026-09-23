// @vitest-environment happy-dom
/* Everything that sits at the body rather than inside a page, and the order it
 * sits in.
 *
 * One step of the `--z` ladder in src/styles/page.css is a deliberate tie --
 * `--z-shade` with `--z-tip` at 90 -- so for those two elements the DOM order
 * at the body is the whole of the stacking decision. The order is declared in
 * src/state/portals.ts, which is also what hands out the four standing layers,
 * and if that table says one thing while the page does another the tie flips
 * with no test and no pixel to catch it. This file reads the table against the
 * page: the boot goldens, the markup, the popover that stays in its card, and
 * the body while every overlay is up. The table's own accessor has its own
 * test beside it (src/state/portals.test.ts).
 */
// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'
import { describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../i18n/t'
import * as session from '../lib/session'
import * as confirm from '../state/confirm'
import * as menu from '../state/menu'
import * as perm from '../state/perm'
import { BOOT_BODY_ORDER, LAYERS, PORTALS, _resetForTests as resetLayers, host } from '../state/portals'
import { resetSources } from '../state/sources'
import * as toast from '../state/toast'
import * as tip from '../state/tooltip'
import * as upgrade from '../state/upgradeShade'
import { bodySiblings } from './domSnapshot'
import { mountPageRoot } from './pageRoot'


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

/* What page.html still carries, which is the two pre-JavaScript shells and the
   onboarding host. Every other region is the page root's, so a case that reads
   one mounts that root over this. */
function pageMarkup(): Document {
  const html = source('src/page.html')
  const body = html
    .slice(html.indexOf('<body>') + '<body>'.length, html.indexOf('</body>'))
    .replace(/<script[\s\S]*?<\/script>/g, '')
  document.body.innerHTML = body
  return document
}

/* The page as it stands once the root has rendered and the two shells are gone,
   which is the state the boot goldens record. */
function bootedPage(): { doc: Document; unmount: () => void } {
  const doc = pageMarkup()
  doc.getElementById('splash')!.remove()
  doc.getElementById('noJs')!.remove()
  return { doc, unmount: mountPageRoot() }
}

describe('the portal table', () => {
  it('lists eleven hosts and no two of them twice', () => {
    expect(PORTALS).toHaveLength(11)
    expect(new Set(PORTALS.map((p) => p.id)).size).toBe(11)
  })

  /* Standing hosts: at the body from the page root's first commit, and the
     writer only fills them. Where they sit among the body's children is what
     decides the two ties, which is why they are in the table at all. */
  it('places the two standing hosts at the body, the page root having rendered', () => {
    const { doc, unmount } = bootedPage()
    try {
      for (const id of ['menu', 'toasts']) {
        const el = doc.getElementById(id)
        expect(el, `#${id} is not rendered`).toBeTruthy()
        expect(el!.parentElement).toBe(doc.body)
      }
    } finally {
      unmount()
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
      expect(stub[portal.at - 1], `${portal.id} is not body child ${portal.at}`)
        .toBe(portal.bootKey)
    }
  })

  it('gives the tooltip layer a place before the update shade, breaking the tie at 90', () => {
    /* page.css says the tie is deliberate and reads as if a tooltip may sit on
       the shade; the DOM says otherwise, because .tipp is appended while the
       page installs and .upshade only when an upgrade starts. The table copies
       what is measured, not what the comment intends. */
    expect(BOOT_BODY_ORDER.indexOf('div.tipp')).toBeGreaterThan(-1)
    expect(PORTALS.find((p) => p.id === '.tipp')!.at).toBe(13)
    expect(PORTALS.find((p) => p.id === '.upshade')!.at).toBe('last')
  })

  /* The table says it; this is the page doing it. Both are at the body at
     once, which is the only moment the tie at 90 is decided by anything. */
  it('keeps the tooltip layer under the update shade on the page itself', () => {
    let unmount = (): void => {}
    try {
      const doc = pageMarkup()
      resetLayers()
      tip.mount()
      unmount = mountPageRoot()
      void doc
      const shade = upgrade.open()
      shade.say('Working')
      const order = bodySiblings(doc)
      const pill = order.findIndex((line) => line.startsWith('div.tipp'))
      const up = order.findIndex((line) => line.startsWith('div.upshade'))
      expect(pill, 'the tooltip layer is not at the body').toBeGreaterThan(-1)
      expect(up, 'the update shade is not at the body').toBeGreaterThan(-1)
      expect(pill).toBeLessThan(up)
    } finally {
      upgrade._resetForTests()
      tip._resetForTests()
      unmount()
      resetLayers()
    }
  })

  /* The scrollbar layer is the first of the four, whatever order they are
     asked for in -- which is what keeps the picker under the two popovers and
     the pill under the shade no matter which module asks first. */
  it('hands out the four standing layers in the table\'s order, not the asking\'s', () => {
    const doc = pageMarkup()
    try {
      resetLayers()
      for (const layer of [...LAYERS].reverse()) host(layer)
      expect(bodySiblings(doc).slice(-4)).toEqual(['div.sbars', 'div', 'div#deskHost', 'div.tipp'])
    } finally {
      resetLayers()
    }
  })

  it('gives the model picker its place among the standing layers, and reparents nothing', () => {
    const picker = PORTALS.find((p) => p.id === 'pickHost')!
    expect(picker.at).toBe(11)
    /* The composer's popovers hang off their chips with the stylesheet now, so
       no portal leaves the card it was born in. */
    expect(PORTALS.filter((p) => p.kind === 'reparent')).toEqual([])
  })

  /* The page's own root appends its regions to the body, after the three
     things page.html still carries: it is a portal, and a root AT the body
     would clear that markup instead of joining it (see src/main.tsx). Two of
     the three are taken down at boot, which leaves #onb and the sixteen
     regions -- the golden's whole static half. */
  it('renders the page regions at the body, in the boot golden order', () => {
    const { doc, unmount } = bootedPage()
    try {
      expect(bodyKeys(doc)).toEqual(BOOT_BODY_ORDER.slice(0, -4))
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
      /* The two pre-JavaScript shells are taken down at boot, which is why the
         golden's static half is seventeen regions rather than nineteen. */
      const doc = pageMarkup()
      doc.getElementById('splash')!.remove()
      doc.getElementById('noJs')!.remove()
      unmount = mountPageRoot()
      resetLayers()
      for (const layer of LAYERS) host(layer)
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

  it('renders the permission popover inside the composer card, beside its chip', () => {
    const { doc, unmount } = bootedPage()
    try {
      const el = doc.getElementById('permPop')
      expect(el, '#permPop is not rendered').toBeTruthy()
      expect(el!.closest('.dock-in')).not.toBeNull()
      expect(el!.previousElementSibling!.id).toBe('permChip')
    } finally {
      unmount()
    }
  })
})

/* The popover stays put, on the live module rather than on the markup: the
   permission popover is rendered beside its chip (src/chrome/PermPopover.tsx)
   and hangs off it with the stylesheet, so an open moves nothing. */
describe('a popover that has been opened', () => {
  let unmount = (): void => {}

  async function open(): Promise<HTMLElement> {
    setTranslator((key) => key)
    document.body.innerHTML = '<div class="dock"></div>'
    perm._resetForTests()
    session._resetForTests()
    session.setCurrent('cli:one')
    unmount = mountPageRoot()
    perm.open()
    return document.getElementById('permPop')!
  }

  function reset(): void {
    unmount()
    unmount = () => {}
    resetTranslator()
    document.body.innerHTML = ''
    perm._resetForTests()
    session._resetForTests()
    resetSources()
  }

  it('stays in the card, open and closed, and adds nothing to the body', async () => {
    try {
      const pop = await open()
      expect(pop.dataset.open).toBe('true')
      expect(pop.closest('.dock-in')).not.toBeNull()
      expect(bodySiblings().some((line) => line.startsWith('div#permPop'))).toBe(false)
      perm.close()
      expect(pop.closest('.dock-in')).not.toBeNull()
      expect(pop.dataset.open).toBe('false')
    } finally {
      reset()
    }
  })
})
