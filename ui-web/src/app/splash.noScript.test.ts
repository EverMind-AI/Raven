// @vitest-environment happy-dom
/* The no-script notice, and the one property no other region of the page needs.
 *
 * Beside splash.ts because that module owns both pre-JavaScript shells, and
 * #noJs is the one whose whole audience is a reader whose browser never ran the
 * bundle: dropNoJs takes it down as one of the first things src/main.tsx does,
 * so a reader still looking at it is a reader for whom the message catalogue
 * (src/i18n/t.ts) and the resolver (src/state/lang/store.ts) have not run and
 * cannot run. Nothing else can reach it either -- <html lang> is served as zh-CN
 * to everyone, so a CSS :lang() rule has one answer for both readers, and the
 * notice's own advice is to save the file and open it locally, which puts the
 * reader on file:// with no server left to negotiate a language.
 *
 * So the one construction that works is both languages present in the markup at
 * once, each block declaring which one it is. These cases read the served
 * skeleton rather than a booted page, because the skeleton is the whole of what
 * the reader in question ever gets -- which is also why they are here rather
 * than in splash.test.ts, whose cases all drive the module against a document.
 *
 * Shape is pinned in src/test/regions.test.ts, off the same file; what is here
 * is only the property that shape cannot express.
 */
// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

/* Han, kana and the full-width forms -- the same run the repo's own gate looks
   for (scripts/check_source_language.py), written as escapes so this file stays
   ASCII the way AGENTS.md 1.3 asks of everything under ui-web/src. */
const CJK = /[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]/

/* A sentence rather than a word: the Chinese copy already carries `JavaScript`,
   `Chrome` and `Safari`, so "there are latin letters in here" is a test that
   passes on a notice with no English in it at all. */
const SENTENCE = 40

/** #noJs as the document is served with it, parsed rather than booted. */
function notice(): Element {
  const html = readFileSync('src/page.html', 'utf8') as string
  const el = new DOMParser().parseFromString(html, 'text/html').getElementById('noJs')
  expect(el, 'src/page.html no longer carries #noJs').toBeTruthy()
  return el!
}

/* The blocks written in one language, by the tag each declares for itself.
   Both declare one, including the Chinese block the document's own <html lang>
   would otherwise cover: the notice has to say which language each half is in
   without depending on what the document was served declaring. */
const written = (root: Element, prefix: string): Element[] =>
  Array.from(root.querySelectorAll('[lang]')).filter((el) =>
    (el.getAttribute('lang') ?? '').toLowerCase().startsWith(prefix),
  )

const text = (blocks: Element[]): string => blocks.map((el) => el.textContent ?? '').join(' ').trim()

describe('the no-script notice', () => {
  it('carries a copy marked as English, with no Chinese left in it', () => {
    const blocks = written(notice(), 'en')
    expect(blocks.length, 'no element inside #noJs declares lang="en"').toBeGreaterThan(0)
    expect(text(blocks)).not.toMatch(CJK)
    expect(text(blocks).length).toBeGreaterThanOrEqual(SENTENCE)
  })

  it('carries a copy marked as Chinese', () => {
    const blocks = written(notice(), 'zh')
    expect(blocks.length, 'no element inside #noJs declares lang="zh-CN"').toBeGreaterThan(0)
    expect(text(blocks)).toMatch(CJK)
    expect(text(blocks).length).toBeGreaterThanOrEqual(SENTENCE)
  })
})

/* The stylesheet's own ladder, by token name. */
function ladder(): Record<string, number> {
  const css = readFileSync('src/styles/page.css', 'utf8') as string
  const out: Record<string, number> = {}
  for (const m of css.matchAll(/--z-([a-z-]+):[ ]*([0-9]+);/g)) out[m[1]!] = Number(m[2])
  return out
}

/** The `animation` shorthand page.css gives #noJs, as written. */
function animation(): string {
  const css = readFileSync('src/styles/page.css', 'utf8') as string
  const rule = /#noJs[ ]*\{([^}]*)\}/.exec(css)
  expect(rule, 'src/styles/page.css carries no #noJs rule').toBeTruthy()
  const decl = /animation:([^;]+);/.exec(rule![1]!)
  expect(decl, '#noJs declares no animation').toBeTruthy()
  return decl![1]!.trim()
}

/* Longer than evaluating the bundle takes on a slow machine. app/splash.ts
   removes #noJs as main.tsx starts, so this delay is the whole of what keeps a
   reader whose bundle DID run from seeing an amber bar over the splash. */
const BOOT_HEADROOM = 2

/** A CSS time, in seconds. */
const seconds = (v: string): number => (v.endsWith('ms') ? Number(v.slice(0, -2)) / 1000 : Number(v.slice(0, -1)))

describe('the no-script notice against the splash', () => {
  /* Only a browser can show the pixels: happy-dom has no compositor, so what is
     pinned here is the stacking decision, not the paint. Measured in headless
     Chrome with no script: zero amber pixels before this, full bar after. */
  it('outranks the splash that would otherwise paint over it', () => {
    const z = ladder()
    expect(z.noscript, 'the ladder declares no --z-noscript').toBeGreaterThan(0)
    expect(z.noscript).toBeGreaterThan(z.splash!)
  })

  it('reaches that step through the animation #noJs runs', () => {
    const name = animation().split(/[ ]+/)[0]!
    const css = readFileSync('src/styles/page.css', 'utf8') as string
    const at = css.indexOf(`@keyframes ${name}`)
    expect(at, `no @keyframes ${name}`).toBeGreaterThan(-1)
    expect(css.slice(at, at + 300)).toContain('var(--z-noscript)')
  })

  it('waits longer than a boot takes before it shows', () => {
    const shorthand = animation()
    /* Two time values in the shorthand, and the SECOND is the delay -- the
       first is the duration, which is zero here. */
    const times = shorthand.match(/[0-9.]+m?s/g) ?? []
    expect(times.length, 'the animation names a duration but no delay').toBeGreaterThanOrEqual(2)
    expect(shorthand, 'without a forwards fill the raise does not persist').toMatch(/forwards|both/)
    expect(seconds(times[1]!)).toBeGreaterThanOrEqual(BOOT_HEADROOM)
  })
})
