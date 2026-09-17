/* Every island follows the page's language, because its root component
 * subscribes to the store that holds it.
 *
 * A language pick used to be a hand-written list of island redraws
 * (state/lang/effects.ts), and the list was incomplete by construction: seven
 * domains that call t() were never in it, so a flip left them in the language
 * before it -- for as long as the reader kept the page open. The chrome had the
 * answer all along (seventeen components read `useSyncExternalStore(lang.subscribe,
 * lang.get)`); this holds each island's root to the same line, so a new domain
 * follows a flip by existing rather than by being remembered.
 *
 * A domain with no root of its own is named below with the reason: what it
 * renders is somebody else's tree, or it is mounted per lane and keeps the one
 * imperative repaint the effects list still carries.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const FEATURES = new URL('../../src/features/', import.meta.url).pathname

/* Either spelling of the subscription: the store as a namespace, or the two
   named imports the memory page and the settings dialog read it through. */
const SUBSCRIBES = [
  /useSyncExternalStore\(\s*lang\.subscribe\s*,/,
  /useSyncExternalStore\(\s*langSubscribe\s*,/,
]

/* Domains with no root component of their own, one reason each. */
const NO_ROOT = {
  composer:
    'the dock is chrome (src/chrome/Dock.tsx), which subscribes there; what this '
    + 'domain owns are the sheets a turn raises, each rendered inside it',
  dag:
    'the graph is a sheet in the rack, mounted per run and rebuilt from the run '
    + 'it is given, so a flip re-renders it through its own mount',
  installed:
    'no island at all: one shared read of ext.list that the two capability tabs '
    + 'and the settings dialog draw from',
  transcript:
    'one root per lane rather than one per page, and its words are baked into '
    + 'stored segments -- which is why the version bump per lane is the one '
    + 'island step state/lang/effects.ts still spends',
}

/** Every `features/<domain>/` directory, and the files in it. */
function domains() {
  const out = new Map()
  for (const name of readdirSync(FEATURES)) {
    const path = join(FEATURES, name)
    if (!statSync(path).isDirectory()) continue
    out.set(name, readdirSync(path).filter((file) => /\.tsx$/.test(file) && !file.includes('.test.')))
  }
  return out
}

/** The body of one exported function, as far as its closing brace at column 0. */
function body(text, name) {
  const at = text.indexOf(`export function ${name}(`)
  if (at === -1) return ''
  const end = text.indexOf('\n}\n', at)
  return text.slice(at, end === -1 ? text.length : end)
}

/** Every `<Domain>App` a domain exports, with the file it is in. */
function roots(domain, files) {
  const found = []
  for (const file of files) {
    const text = readFileSync(join(FEATURES, domain, file), 'utf8')
    for (const match of text.matchAll(/^export function (\w+App)\(/gm)) {
      found.push({ file, name: match[1], body: body(text, match[1]) })
    }
  }
  return found
}

describe('an island following the language', () => {
  it('subscribes from every island root', () => {
    const silent = []
    for (const [domain, files] of domains()) {
      for (const root of roots(domain, files)) {
        if (SUBSCRIBES.some((form) => form.test(root.body))) continue
        silent.push(`features/${domain}/${root.file}: ${root.name}`)
      }
    }
    expect(silent, 'add useSyncExternalStore(lang.subscribe, lang.get) to the root component')
      .toEqual([])
  })

  it('names a root for every domain, or a reason for having none', () => {
    const missing = []
    for (const [domain, files] of domains()) {
      if (roots(domain, files).length) {
        if (domain in NO_ROOT) missing.push(`${domain} has a root now: take it off NO_ROOT`)
        continue
      }
      const why = NO_ROOT[domain]
      if (!why) missing.push(`${domain} has no <Domain>App and no reason in NO_ROOT`)
      else if (why.length < 20) missing.push(`${domain} has no reason`)
    }
    expect(missing).toEqual([])
  })

  it('pins no domain that is gone', () => {
    const present = new Set(domains().keys())
    expect(Object.keys(NO_ROOT).filter((domain) => !present.has(domain))).toEqual([])
  })
})
