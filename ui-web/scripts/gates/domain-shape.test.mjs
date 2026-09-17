/* Every domain has the same files, and each of them means one thing.
 *
 * `features/<domain>/` is where a person adding a feature starts, and what they
 * find there is what they copy. The four below are the shape the tree already
 * mostly has -- the contract types, everything the domain knows about the
 * gateway, its state, and the one declaration the page reads it through -- plus
 * a root component for every domain the page mounts as one piece. Nothing
 * enforced it, so one domain's source lived in another's directory and one had
 * no declaration at all.
 *
 * A ratchet in the house style: EXCEPTIONS is what the tree holds today, one
 * reason per missing file, and it may only shrink. A new domain has them all.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

const FEATURES = new URL('../../src/features/', import.meta.url).pathname

/** The files a domain declares itself with. */
const REQUIRED = ['types.ts', 'source.ts', 'store.ts', 'manifest.ts']

/* What a domain is missing today, and why. Down or gone: the way off this list
   is the file, never another line here. */
const EXCEPTIONS = {
  'composer/source.ts':
    'the dock\'s seam is assembled by the page: the palette half in app/install.ts and the '
    + 'rest by the settings chrome, because no transport answers either half',
  'dag/source.ts': 'answers no seam of its own -- a graph arrives on the turn\'s own events',
  'installed/types.ts': 'no island: one shared read, whose row shapes are the two hubs\' own types',
  'installed/store.ts': 'no island: nothing to hold between two reads of ext.list',
}

/* The root component each domain exports, where its name is not `<Domain>App`
   yet. Every one of these is a name the rename step converges, and the
   directory is what it converges on. */
const NAMES = {
  connections: ['ConnApp'],
  model: ['ModelPickerApp'],
  plugins: ['PlugApp'],
  workspace: ['DeskApp', 'WsApp'],
}

/* Domains with no root component of their own, one reason each. */
const NO_ROOT = {
  composer: 'the dock is chrome (src/chrome/Dock.tsx); what this domain renders are the sheets above it',
  dag: 'a sheet in the rack above the composer, mounted per run (features/dag/mount.tsx)',
  installed: 'no island at all: the capability tabs and the settings dialog draw its rows',
  transcript: 'one root per conversation lane rather than one per page (features/transcript/mount.tsx)',
}

/** Every `features/<domain>/` directory, with the files in it. */
function domains() {
  const out = new Map()
  for (const name of readdirSync(FEATURES)) {
    const path = join(FEATURES, name)
    if (!statSync(path).isDirectory()) continue
    out.set(name, readdirSync(path))
  }
  return out
}

/** Every `<Something>App` a domain exports, with the file it is in. */
function roots(domain, files) {
  const found = []
  for (const file of files.filter((name) => /\.tsx$/.test(name) && !name.includes('.test.'))) {
    const text = readFileSync(join(FEATURES, domain, file), 'utf8')
    for (const match of text.matchAll(/^export function (\w+App)\(/gm)) found.push({ file, name: match[1] })
  }
  return found
}

const expected = (domain) => `${domain[0].toUpperCase()}${domain.slice(1)}App`

describe('the shape of a domain', () => {
  it('gives every domain its files, or a reason for the one it lacks', () => {
    const gaps = []
    for (const [domain, files] of domains()) {
      for (const what of REQUIRED) {
        if (files.includes(what)) continue
        const why = EXCEPTIONS[`${domain}/${what}`]
        if (!why) gaps.push(`features/${domain}/ has no ${what}`)
        else if (why.length < 20) gaps.push(`features/${domain}/${what} has no reason`)
      }
    }
    expect(gaps, 'add the file, or pin it in EXCEPTIONS with the reason it has none').toEqual([])
  })

  it('gives every domain a root component, or a reason for having none', () => {
    const gaps = []
    for (const [domain, files] of domains()) {
      if (roots(domain, files).length) {
        if (domain in NO_ROOT) gaps.push(`${domain} has a root now: take it off NO_ROOT`)
        continue
      }
      const why = NO_ROOT[domain]
      if (!why) gaps.push(`features/${domain}/ exports no <Domain>App and has no reason in NO_ROOT`)
      else if (why.length < 20) gaps.push(`${domain} has no reason`)
    }
    expect(gaps).toEqual([])
  })

  it('names the root after the domain, or pins the name the rename will take', () => {
    const wrong = []
    for (const [domain, files] of domains()) {
      const names = roots(domain, files).map((found) => found.name)
      const off = names.filter((name) => name !== expected(domain))
      const pinned = NAMES[domain] ?? []
      for (const name of off) {
        if (!pinned.includes(name)) wrong.push(`features/${domain}/: ${name} (not ${expected(domain)})`)
      }
      for (const name of pinned) {
        if (!names.includes(name)) wrong.push(`features/${domain}/: ${name} is gone from NAMES`)
      }
    }
    expect(wrong, 'name it after the directory, or pin it with the rename it is waiting for').toEqual([])
  })

  it('pins nothing that is gone', () => {
    const present = domains()
    const stale = []
    for (const key of Object.keys(EXCEPTIONS)) {
      const at = key.indexOf('/')
      const [domain, what] = [key.slice(0, at), key.slice(at + 1)]
      const files = present.get(domain)
      if (!files) { stale.push(`${key}: no such domain`); continue }
      if (files.includes(what)) stale.push(`${key}: the file is there now`)
    }
    for (const domain of [...Object.keys(NAMES), ...Object.keys(NO_ROOT)]) {
      if (!present.has(domain)) stale.push(`${domain}: no such domain`)
    }
    expect(stale, 'delete the healed pins').toEqual([])
  })
})
