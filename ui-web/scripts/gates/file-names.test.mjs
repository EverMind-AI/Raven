/* What a file is called, and the handful that are called something else.
 *
 * One word per concept only holds if the name is on the file too: a reader
 * looking for the skill store opens features/skills/store.ts, and a reader
 * looking for what pins the escape order opens state/escapeOrder.test.ts. Four
 * rules, all of them already true of nearly every file in the tree:
 *
 *   components  -- a .tsx is PascalCase, after the component it renders
 *   modules     -- everything else is camelCase
 *   tests       -- <module>.test.ts, or <module>.<aspect>.test.ts when one
 *                  module needs several, and <module> is a file beside it
 *   gates       -- scripts/gates/<what-it-pins>.test.mjs, kebab-case
 *
 * Nothing enforced any of them, so two kebab-case test files named no module
 * at all and a third filed under a module it does not test.
 *
 * A ratchet in the house style (see state-dom-touch.test.mjs): every list
 * below is what the tree holds today, one reason each, and it may only shrink.
 */

import { readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { describe, expect, it } from 'vitest'

const SRC = new URL('../../src/', import.meta.url).pathname
const GATES = new URL('.', import.meta.url).pathname

/* A .tsx that renders nothing, so its name is a module's and not a
   component's. The way off this list is the file's own name. */
const NOT_COMPONENTS = {
  'main.tsx': 'the entry point: it mounts the roots the manifests declare and exports nothing',
  'features/browser/mount.tsx': 'the browser view\'s root handling -- it holds JSX but exports verbs',
  'features/composer/mount.tsx': 'the dock\'s four roots and the verbs the page\'s machinery calls',
  'features/dag/mount.tsx': 'the graph sheet\'s root, mounted per run rather than per page',
  'features/subagents/mount.tsx': 'the agents view\'s root handling, the browser view\'s twin',
  'features/transcript/mount.tsx': 'one root per conversation lane, and the lane bookkeeping',
}

/* A test that is about the whole assembled page rather than one module, so
   there is no module beside it to be named after. */
const PAGE_SUITES = {
  'test/portals.test.ts': 'every layer standing at the body, read off the booted page',
  'test/regions.test.ts': 'every region the page root renders, against its golden',
}

/* Declaration files, which carry the tool's name rather than a module's. */
const NOT_MODULES = new Set(['vite-env.d.ts'])

const PASCAL = /^[A-Z][A-Za-z0-9]*$/
const CAMEL = /^[a-z][A-Za-z0-9]*$/
const KEBAB = /^[a-z0-9]+(-[a-z0-9]+)*$/

function* files(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) yield* files(path)
    else yield path
  }
}

/** Every file under src/, by its path from src/. */
const tree = [...files(SRC)].map((path) => relative(SRC, path)).sort()
const beside = (rel) => new Set(tree.filter((other) => other.replace(/[^/]+$/, '') === rel.replace(/[^/]+$/, '')))

const isTest = (rel) => /\.test\.tsx?$/.test(rel)
const base = (rel) => rel.split('/').pop()

describe('what a file is called', () => {
  it('names every component file after the component it renders', () => {
    const wrong = []
    for (const rel of tree) {
      if (!rel.endsWith('.tsx') || isTest(rel)) continue
      if (NOT_COMPONENTS[rel]) continue
      const name = base(rel).replace(/\.tsx$/, '')
      if (!PASCAL.test(name)) wrong.push(rel)
    }
    expect(wrong, 'a .tsx is PascalCase, or it is pinned in NOT_COMPONENTS with what it holds instead')
      .toEqual([])
  })

  it('names every other module in camelCase', () => {
    const wrong = []
    for (const rel of tree) {
      if (!/\.tsx?$/.test(rel) || isTest(rel)) continue
      if (NOT_MODULES.has(base(rel))) continue
      const name = base(rel).replace(/\.tsx?$/, '')
      /* A component file is the case above's; only the pinned .tsx land here. */
      if (rel.endsWith('.tsx') && !NOT_COMPONENTS[rel]) continue
      if (!CAMEL.test(name)) wrong.push(rel)
    }
    expect(wrong, 'camelCase, one word per concept').toEqual([])
  })

  it('names every test after a module beside it', () => {
    const wrong = []
    for (const rel of tree) {
      if (!isTest(rel)) continue
      if (PAGE_SUITES[rel]) continue
      const stem = base(rel).replace(/\.test\.tsx?$/, '')
      const parts = stem.split('.')
      if (parts.length > 2) { wrong.push(`${rel}: one aspect at most`); continue }
      const [module, aspect] = parts
      const siblings = beside(rel)
      const dir = rel.replace(/[^/]+$/, '')
      const found = ['ts', 'tsx'].some((ext) => siblings.has(`${dir}${module}.${ext}`))
      if (!found) { wrong.push(`${rel}: no ${module}.ts(x) beside it`); continue }
      if (aspect !== undefined && !CAMEL.test(aspect)) wrong.push(`${rel}: ${aspect} is not an aspect`)
    }
    expect(wrong, '<module>.test.ts, or <module>.<aspect>.test.ts beside the module it is about')
      .toEqual([])
  })

  it('names every gate after what it pins', () => {
    const wrong = []
    for (const name of readdirSync(GATES)) {
      if (!name.endsWith('.test.mjs')) continue
      if (!KEBAB.test(name.replace(/\.test\.mjs$/, ''))) wrong.push(name)
    }
    expect(wrong, 'scripts/gates/<what-it-pins>.test.mjs, kebab-case').toEqual([])
  })

  it('pins nothing that is gone, and nothing that is fine now', () => {
    const stale = []
    for (const [rel, why] of Object.entries({ ...NOT_COMPONENTS, ...PAGE_SUITES })) {
      if (!tree.includes(rel)) { stale.push(`${rel}: no such file`); continue }
      if (why.length < 20) stale.push(`${rel}: no reason`)
    }
    for (const rel of Object.keys(NOT_COMPONENTS)) {
      if (PASCAL.test(base(rel).replace(/\.tsx$/, ''))) stale.push(`${rel}: PascalCase now, take it off`)
    }
    for (const rel of Object.keys(PAGE_SUITES)) {
      const module = base(rel).replace(/\.test\.tsx?$/, '').split('.')[0]
      const dir = rel.replace(/[^/]+$/, '')
      if (['ts', 'tsx'].some((ext) => tree.includes(`${dir}${module}.${ext}`))) {
        stale.push(`${rel}: ${module} is beside it now, take it off`)
      }
    }
    for (const name of NOT_MODULES) {
      if (!tree.some((rel) => base(rel) === name)) stale.push(`${name}: no such file`)
    }
    expect(stale, 'delete the healed pins').toEqual([])
  })
})
