// @vitest-environment happy-dom
/* Everything that sits at the body rather than inside a page, and the order it
 * sits in.
 *
 * Two steps of the `--z` ladder in src/styles/page.css are deliberate ties --
 * `--z-shade` with `--z-tip` at 90, and `--z-picker` with the inline 46 the two
 * composer popovers set -- so for those four elements the DOM order at the body
 * is the whole of the stacking decision. Stage C renders these containers from
 * one place (state/portals.ts), and if that place emits them in another order
 * the ties flip with no test and no pixel to catch it. This file is that test.
 *
 * Three kinds are distinguished, because each breaks differently:
 *   static   -- page.html already has it at the body; the writer only fills it.
 *   reparent -- born inside a page, moved to the body on first open, and never
 *               moved back.
 *   append   -- created at runtime and appended to the body.
 */
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'
import { flushSync } from 'react-dom'
import { describe, expect, it } from 'vitest'

// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'

import { App } from '../App'
import * as session from '../shell/session'
import * as tier from '../shell/tier'
import { resetShell, setShell } from '../shell/bridge'
import { resetSources, setSources } from '../state/sources'
import { bodySiblings } from './domSnapshot'

import type { Shell } from '../shell/bridge'
import type { TierReply, TierSource } from '../shell/tier'

interface Portal {
  /** What the element is called; a CSS selector where it has one to itself. */
  readonly selector: string
  readonly kind: 'static' | 'reparent' | 'append'
  /** The `--z` token it takes, or the literal an inline style sets. */
  readonly z: string
  /** Its 1-based place among the body's children at boot, or `last` for the
   *  ones that only reach the body when something opens them. */
  readonly at: number | 'last'
  /** The line a boot golden shows for it, for the four that are there at boot
   *  and the two static hosts. */
  readonly bootKey?: string
}

/* The thirteen, in the order the design's portal table lists them. */
const PORTALS: readonly Portal[] = [
  { selector: '.sbars', kind: 'append', z: '--z-scrollbars', at: 18, bootKey: 'div.sbars' },
  { selector: 'pickHost', kind: 'append', z: '--z-picker', at: 19, bootKey: 'div' },
  { selector: '#deskHost', kind: 'append', z: '--z-desk', at: 20, bootKey: 'div#deskHost' },
  { selector: '.tipp', kind: 'append', z: '--z-tip', at: 21, bootKey: 'div.tipp' },
  { selector: '#permPop', kind: 'reparent', z: '46', at: 'last' },
  { selector: '#tierPop', kind: 'reparent', z: '46', at: 'last' },
  { selector: 'button.lightbox', kind: 'append', z: '--z-lightbox', at: 'last' },
  { selector: '.upshade', kind: 'append', z: '--z-shade', at: 'last' },
  { selector: '.topfail', kind: 'append', z: '--z-failbar', at: 'last' },
  { selector: 'bootErrorBar', kind: 'append', z: '99', at: 'last' },
  { selector: 'input[type=file]', kind: 'append', z: '', at: 'last' },
  { selector: '#menu', kind: 'static', z: '--z-menu', at: 16, bootKey: 'div#menu' },
  { selector: '#toasts', kind: 'static', z: '--z-toast', at: 17, bootKey: 'div#toasts' },
]

/* The body's standing order after boot, as the goldens record it: seventeen
   static regions (#splash and #noJs are removed before the snapshot is taken)
   then the four appended while the page installs itself. Keyed by tag plus id,
   or tag plus classes when there is no id -- the model picker's wrapper has
   neither, which is why one entry is a bare `div`. */
const BOOT_BODY_ORDER = [
  'div#onb',
  'div.app',
  'button#railShow',
  'section#capsPage',
  'section#xaPage',
  'section#connPage',
  'section#memPage',
  'section#pbPage',
  'section#kbPage',
  'section#cronPage',
  'div#jobVeil',
  'aside#detail',
  'div#setVeil',
  'div#veil',
  'div#connVeil',
  'div#menu',
  'div#toasts',
  'div.sbars',
  'div',
  'div#deskHost',
  'div.tipp',
] as const

const source = (path: string): string => readFileSync(path, 'utf8') as string

/* The body-level lines of a boot golden, reduced to the key BOOT_BODY_ORDER
   uses: the golden's own bytes are already pinned by scripts/boot-snapshot.mjs,
   so what is read here is only the order. */
function goldenBodyKeys(path: string): string[] {
  return source(path)
    .split('\n')
    .filter((line) => line.length > 0 && !line.startsWith(' '))
    .map((line) => {
      const parsed = /^([a-z]+)(?:#([\w-]+))?((?:\.[^.[]+)*)/.exec(line)
      expect(parsed, `unreadable body-level line in ${path}: ${line}`).toBeTruthy()
      const [, tag = '', id, classes = ''] = parsed!
      if (id) return `${tag}#${id}`
      return `${tag}${classes}`
    })
}

/* page.html's two static hosts and the card the two popovers start inside. */
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
    const root = createRoot(document.createElement('div'))
    flushSync(() => root.render(createElement(App)))
    expect(bodySiblings(doc)).toEqual(before)
  })

  it('starts the two popovers inside the composer card', () => {
    const doc = pageMarkup()
    for (const id of ['permPop', 'tierPop']) {
      const el = doc.getElementById(id)
      expect(el, `#${id} is not in page.html`).toBeTruthy()
      expect(el!.parentElement!.className).toBe('dock-in')
    }
  })
})

/* The reparenting itself, on the live module rather than on the markup: the
   fixture is tier.test.ts's, because the card is what the panel has to open
   clear of and `.dock-in` is that card. */
describe('a popover that has been opened', () => {
  const MENU = [
    { id: 'medium', name: 'Medium', description: 'The least effort a sub-agent is asked for.' },
    { id: 'high', name: 'High', description: 'The middle amount of effort, between the other two.' },
    { id: 'max', name: 'Max', description: 'The most effort a sub-agent is asked for.' },
  ]

  function markup(): void {
    document.body.innerHTML = `
      <div class="dock-in">
        <button class="chip" id="tierChip" aria-expanded="false" aria-haspopup="true" hidden>
          <svg class="pico" viewBox="0 0 24 24" aria-hidden="true"></svg>
          <span id="tierName"></span>
        </button>
        <div class="pop" id="tierPop" data-open="false">
          <div class="hd"><span class="lab"></span></div>
          <div id="tierList" role="radiogroup"></div>
          <div class="note"></div>
        </div>
      </div>`
  }

  async function open(): Promise<HTMLElement> {
    setShell({ T: (key) => key, confirmAsk: () => {}, showPage: () => {} } as Shell)
    markup()
    tier._resetForTests()
    session._resetForTests()
    session.setCurrent('cli:one')
    const src: TierSource = {
      read: async (): Promise<TierReply> => ({ mode: 'high', availableModes: MENU }),
      set: async (mode): Promise<TierReply> => ({ mode: mode ?? 'high', availableModes: MENU }),
    }
    setSources({ tier: src })
    await tier.load()
    tier.open()
    return document.getElementById('tierPop')!
  }

  function reset(): void {
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
