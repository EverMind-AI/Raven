// @vitest-environment happy-dom
/* A golden per top-level region of the page, taken from the markup that ships
 * today.
 *
 * Stage C moves every one of these nineteen regions out of src/page.html and
 * into App.tsx, one PR at a time. The promise is that the DOM does not move: an
 * element's tag, id, classes, data-* attributes and the order of its children
 * all stay as they are. So the golden is written once, here, off page.html --
 * and each later step changes only where the test gets the markup from, never
 * the golden text. A region whose owner changed and whose shape changed with it
 * fails on the region it broke, by name.
 *
 * The markup is read and parsed rather than booted: no script runs, so what is
 * pinned is the skeleton the document is served with, before any island or
 * writer has touched it. The boot-time shape has its own gate
 * (scripts/boot-snapshot.mjs) and the body's standing order has another
 * (portals.test.ts).
 */
import { describe, expect, it } from 'vitest'

// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { env } from 'node:process'

import { bodySiblings, elementSnapshot } from './domSnapshot'

/* The nineteen regions, in document order, keyed the way their golden files
   are: by id, or by class for the one region that has no id (`div.app`). */
const REGIONS = [
  'splash',
  'onb',
  'noJs',
  'app',
  'railShow',
  'capsPage',
  'xaPage',
  'connPage',
  'memPage',
  'pbPage',
  'kbPage',
  'cronPage',
  'jobVeil',
  'detail',
  'setVeil',
  'veil',
  'connVeil',
  'menu',
  'toasts',
] as const

const GOLDEN_DIR = 'src/test/__golden__'

/* Read the golden, writing it the first time on a developer machine.
   Never under CI -- a fresh checkout must not be able to mint its own
   expectation -- and never over one that exists unless asked, because
   "regenerate the goldens" is exactly how a real regression gets blessed. */
function golden(name: string, actual: string): string {
  const path = `${GOLDEN_DIR}/region-${name}.txt`
  const exists = existsSync(path) as boolean
  if (env.CI) {
    if (!exists) throw new Error(`regions: golden missing under CI: ${path}`)
  } else if (!exists || env.UPDATE_REGION_GOLDENS === '1') {
    mkdirSync(GOLDEN_DIR, { recursive: true })
    writeFileSync(path, `${actual}\n`)
  }
  return (readFileSync(path, 'utf8') as string).replace(/\n$/, '')
}

/* page.html's body, minus the two inline <script> blocks. They are the only
   thing in there that is not markup, and build.py is what decides how many of
   them a built page carries. */
function pageBody(): string {
  const html = readFileSync('src/page.html', 'utf8') as string
  const open = html.indexOf('<body>')
  const close = html.indexOf('</body>')
  expect(open).toBeGreaterThan(-1)
  expect(close).toBeGreaterThan(open)
  return html.slice(open + '<body>'.length, close).replace(/<script[\s\S]*?<\/script>/g, '')
}

/* Where each region's markup comes from. The C2..C13 steps replace the body of
   this function, region by region, with a render of App.tsx -- the goldens
   above do not change with it. */
function render(): Document {
  document.body.innerHTML = pageBody()
  return document
}

/** The key a region is filed under: its id, or its first class. */
const keyOf = (el: Element): string =>
  el.id || (el.getAttribute('class') ?? '').trim().split(/\s+/)[0] || el.tagName.toLowerCase()

describe('the page skeleton', () => {
  it('has exactly the nineteen top-level regions, in order', () => {
    const doc = render()
    const keys = Array.from(doc.body.children).map(keyOf)
    expect(keys).toEqual([...REGIONS])
  })

  /* The two readings have to agree: portals.test.ts pins the body's standing
     order through `bodySiblings`, the goldens below pin each region through
     `elementSnapshot`, and a region is named the same way by both. */
  it('names each region the same way at the body level as in its own snapshot', () => {
    const doc = render()
    const heads = Array.from(doc.body.children).map((child) => elementSnapshot(child).split('\n')[0] ?? '')
    expect(bodySiblings(doc)).toEqual(heads)
    expect(heads).toHaveLength(REGIONS.length)
  })
})

describe.each(REGIONS)('region %s', (name) => {
  it('renders the DOM its golden records', () => {
    const doc = render()
    const el = Array.from(doc.body.children).find((child) => keyOf(child) === name)
    expect(el, `no top-level region keyed "${name}"`).toBeTruthy()
    const actual = elementSnapshot(el!)
    expect(actual).toBe(golden(name, actual))
  })
})
