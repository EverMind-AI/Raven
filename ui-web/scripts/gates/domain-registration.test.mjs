/* A domain is registered everywhere, or the gate says where it is not.
 *
 * Adding a page used to mean touching nine tables, and six of them failed in
 * silence: a page missing from the rail's mark list had no selected state, one
 * missing from the Escape order could not be taken back, one missing a
 * `createRoot` mounted no island at all -- an empty page, no error. One shipped
 * that way (scripts/gates/rail-nav-registry.test.mjs says which).
 *
 * The tables are derived now: state/pages.ts declares a page and src/App.tsx,
 * state/page.ts, state/escapeOrder.ts, state/portals.ts, chrome/Rail.tsx,
 * features/rail/store.ts and src/test/regions.test.ts all read it, so there is
 * nothing left for a new page to be missing FROM. What this gate holds is the
 * two ends of that: every domain declares itself (features/<domain>/manifest.ts
 * into features/manifests.ts), and every page the table declares is a page some
 * domain owns -- or is the one page chrome renders itself.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const SRC = new URL('../../src/', import.meta.url).pathname

const read = (rel) => readFileSync(join(SRC, rel), 'utf8')

/** Every `features/<domain>/` directory. */
const domains = () =>
  readdirSync(join(SRC, 'features'))
    .filter((name) => statSync(join(SRC, 'features', name)).isDirectory())
    .sort()

/* Read from the source text rather than imported: the manifests pull every
   island's component behind them, and a gate about the tables has no business
   booting the page to read one. */
function manifest(domain) {
  const text = read(`features/${domain}/manifest.ts`)
  const at = text.indexOf('export const manifest')
  if (at === -1) return null
  const body = text.slice(at, text.indexOf('\n}', at))
  const field = (name) => body.match(new RegExp(`\\b${name}:\\s*'([^']+)'`))?.[1]
  const list = (name) => {
    const found = body.match(new RegExp(`\\b${name}:\\s*\\[([^\\]]*)\\]`))
    return found ? [...found[1].matchAll(/'([^']+)'/g)].map((m) => m[1]) : []
  }
  return {
    domain: field('domain'),
    page: field('page'),
    host: field('host'),
    sources: list('sources'),
    root: /\broot:\s*\w/.test(body),
  }
}

/** The page rows state/pages.ts declares, as id -> the row's own text. */
function pages() {
  const text = read('state/pages.ts')
  const table = text.slice(text.indexOf('const DECLARED = ['), text.indexOf('] as const'))
  const out = new Map()
  for (const row of table.matchAll(/\{[^}]*\}/g)) {
    const id = row[0].match(/id:\s*'([^']+)'/)?.[1]
    if (id) out.set(id, row[0])
  }
  return out
}

/** The seam keys the page has, from the one interface that declares them. */
function seamKeys() {
  const text = read('state/sources.ts')
  const at = text.indexOf('export interface Sources {')
  const body = text.slice(at, text.indexOf('\n}', at))
  return [...body.matchAll(/^\s{2}(\w+):/gm)].map((m) => m[1]).sort()
}

describe('a domain registered', () => {
  it('declares itself, once, in features/manifests.ts', () => {
    const table = read('features/manifests.ts')
    const listed = [...table.matchAll(/^import \{ manifest as (\w+) \} from '\.\/([\w-]+)\/manifest'$/gm)]
    expect(listed.map((m) => m[2]).sort()).toEqual(domains())
    /* And the array reads every one of them: an import nothing lists is a
       declaration nothing reads. */
    const array = table.slice(table.indexOf('export const MANIFESTS'))
    const missing = listed.filter((m) => !new RegExp(`^\\s+${m[1]},$`, 'm').test(array))
    expect(missing.map((m) => m[2])).toEqual([])
  })

  it('names itself after its own directory', () => {
    const wrong = []
    for (const domain of domains()) {
      const found = manifest(domain)
      if (!found) { wrong.push(`features/${domain}/manifest.ts declares no manifest`); continue }
      if (found.domain !== domain) wrong.push(`features/${domain}/manifest.ts says '${String(found.domain)}'`)
    }
    expect(wrong).toEqual([])
  })

  it('claims a page the table declares, and every page but one is claimed', () => {
    const table = pages()
    const claimed = new Map()
    const wrong = []
    for (const domain of domains()) {
      const page = manifest(domain)?.page
      if (!page) continue
      if (!table.has(page)) wrong.push(`${domain} claims '${page}', which state/pages.ts does not declare`)
      if (claimed.has(page)) wrong.push(`${page} is claimed by both ${String(claimed.get(page))} and ${domain}`)
      claimed.set(page, domain)
    }
    for (const [id, row] of table) {
      if (claimed.has(id)) continue
      /* The capabilities page is chrome's: two domains draw into it, so it is
         the one page no single domain owns (src/chrome/CapsPage.tsx). */
      if (!/own:\s*true/.test(row)) wrong.push(`${id} is declared but no domain claims it`)
    }
    expect(wrong).toEqual([])
  })

  it('answers seam keys, between them all of them, and none twice', () => {
    const claimed = new Map()
    const wrong = []
    for (const domain of domains()) {
      for (const key of manifest(domain)?.sources ?? []) {
        if (claimed.has(key)) wrong.push(`${key} is claimed by both ${String(claimed.get(key))} and ${domain}`)
        claimed.set(key, domain)
      }
    }
    expect(wrong).toEqual([])
    expect([...claimed.keys()].sort(), 'every domain key on the seam belongs to one domain').toEqual(seamKeys())
  })

  it('mounts every root it declares, through the one loop that does', () => {
    const main = read('main.tsx')
    /* The loop, not a line per island: what it may not be is a table nobody
       reads (nine `if (host)` guards is what it replaced). */
    expect(main).toContain('for (const domain of MANIFESTS)')
    expect(main).toContain('createRoot(box).render(createElement(domain.root))')
    const wrong = []
    for (const domain of domains()) {
      const found = manifest(domain)
      if (!found?.root) continue
      /* A root goes into the page's own body, or into a host it names. */
      if (!found.page && !found.host) wrong.push(`${domain} declares a root with no page and no host`)
    }
    expect(wrong).toEqual([])
  })
})
