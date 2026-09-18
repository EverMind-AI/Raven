/* One store shape, one set of verbs, and one place the notify lives.
 *
 * A store on this page is a module singleton holding one value, made by
 * src/state/store.ts's makeStore: `get` reads the snapshot, `set` writes it and
 * notifies synchronously, `subscribe` is what useSyncExternalStore is given, and
 * `_resetForTests` puts the value back. Every one of those used to be spelled
 * per module -- get or getState or saved or snapshot, set or put or commit, and
 * twenty copies of the flushSync notify -- and the twenty-first copy to forget
 * the flushSync would be a DOM write one tick late, which an imperative reader
 * sees as stale and no test catches.
 *
 * Three ratchets, in the house style: each PINNED list is what the tree holds
 * today and may only shrink. A new module has to land on makeStore; an existing
 * one comes off the list by being moved onto it, never by being added here.
 * Every pin carries the reason it is one, because a pin with no reason reads as
 * a rule nobody meant.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

import { relPath, root } from './paths.mjs'

const SRC = root(new URL('../../src/', import.meta.url))

/** The one module that owns the listener set and the notify. */
const STORE = 'state/store.ts'

/* Modules that export `subscribe` without the rest of the shape. Each holds
   something makeStore's one value and one `set` cannot express. */
const SHAPE = {
  'state/page.ts':
    'its subscribers run AFTER the seven data-open writes and the four effects, '
    + 'which one notifying `set` cannot express; the two tabs read that order',
  'state/session/registry.ts':
    'a map from session key to runtime, not one value: `get(key)` is a lookup',
  'features/browser/store.ts':
    'patch() writes without notifying -- a frame carrying unchanged metadata '
    + 'must cause no render at all, and this view is fed frames',
  'features/dag/store.ts':
    'the notify is an epoch bump over a Map of runs, and `set(key, run)` is the '
    + 'domain verb rather than the store write',
  'features/model/store.ts':
    'three independent reactive values (the open anchor, the epoch, the pick)',
  'features/subagents/store.ts':
    'one write is deliberately quiet: rows whose print matches `drawn` are '
    + 'stored without a redraw',
  'features/transcript/store.ts':
    'a listener set per lane rather than one per module, so a token append '
    + 're-renders the streaming leaf alone',
  'features/workspace/deliveries.ts':
    'four independent reactive values behind one version counter',
}

/* Files that keep a listener set of their own. Two kinds: a store this ratchet
   has not reached yet (the SHAPE list above says why), and a second subscriber
   group that is not a store's snapshot at all. */
const LISTENERS = {
  ...SHAPE,
  'app/connection.ts': 'the connection state watchers, which are app wiring rather than a store',
  'lib/session.ts': 'the session-key watchers, which carry no value of their own',
  'state/caps.ts':
    '`switched`: what the two tabs run after a switch, a second group beside the '
    + "store's own subscribers",
  'state/lang/store.ts':
    '`afterwards`: lang.onApplied, the group that runs after the rendered half '
    + 'has committed',
}

/* Modules with module-level `let` and no reset seam. */
const NO_RESET = {
  'features/browser/mount.tsx': 'the React root it made; a reset would have to unmount it',
  'features/subagents/mount.tsx': 'the React root it made; a reset would have to unmount it',
  'features/transcript/mount.tsx': 'the React roots it made, keyed per lane',
  'features/workspace/deliveries.ts': SHAPE['features/workspace/deliveries.ts'],
}

function* sources(dir, testsToo) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) yield* sources(path, testsToo)
    else if (/\.tsx?$/.test(name) && (testsToo || !name.includes('.test.'))) yield path
  }
}

const modules = (dir) => sources(dir, false)

/** Every non-test module under one of src/'s directories, by its path from src/. */
const layer = (dir) => [...modules(join(SRC, dir))].map((path) => relPath(SRC, path)).sort()

const read = (rel) => readFileSync(join(SRC, rel), 'utf8')

/* What a module exports, as far as a name can be read off the source: a
   declaration, or one of makeStore's four taken by destructuring. */
function exportsOf(rel) {
  const text = read(rel)
  const names = new Set(
    [...text.matchAll(/^export (?:async function|function|const) (\w+)/gm)].map((m) => m[1]),
  )
  for (const block of text.matchAll(/^export const \{([^}]*)\} = store/gm)) {
    for (const name of block[1].split(',')) if (name.trim()) names.add(name.trim())
  }
  return names
}

/* A store's own listener set, in either spelling the tree used before this. */
const KEEPS_LISTENERS = /listeners\.add\(|new Set<\(\) => void>\(\)/

const domain = () => [...layer('state'), ...layer('features')]

describe('the page\'s stores', () => {
  it('gives every module that can be subscribed to the whole shape', () => {
    const wrong = []
    for (const rel of domain()) {
      const names = exportsOf(rel)
      if (!names.has('subscribe')) continue
      const missing = ['get', 'set', '_resetForTests'].filter((verb) => !names.has(verb))
      if (!missing.length) continue
      if (rel in SHAPE) continue
      wrong.push(`${rel}: exports subscribe but not ${missing.join(', ')}`)
    }
    expect(wrong, 'put the module on makeStore (src/state/store.ts), or pin it in SHAPE with the reason')
      .toEqual([])
  })

  it('keeps the listener set and the flushSync notify in one module', () => {
    const wrong = []
    for (const rel of layer('.')) {
      if (rel === STORE || rel in LISTENERS) continue
      if (KEEPS_LISTENERS.test(read(rel))) wrong.push(rel)
    }
    expect(wrong, `a store's notify belongs in ${STORE}; a second subscriber group is pinned in LISTENERS with its reason`)
      .toEqual([])
  })

  it('gives every module that holds state a way back to its first value', () => {
    const wrong = []
    for (const rel of domain()) {
      const text = read(rel)
      if (!/^let /m.test(text) || !/^export /m.test(text)) continue
      if (text.includes('_resetForTests') || rel in NO_RESET) continue
      wrong.push(rel)
    }
    expect(wrong, 'export _resetForTests (or take the state off the module), or pin it in NO_RESET with the reason')
      .toEqual([])
  })

  it('names a reason for every pin, and pins nothing that is gone', () => {
    /* The lists are read by name, so a renamed module would drop out of the
       check silently and a deleted one would leave a pin nothing tests. */
    const present = new Set(layer('.'))
    const missing = []
    for (const [table, rows] of [['SHAPE', SHAPE], ['LISTENERS', LISTENERS], ['NO_RESET', NO_RESET]]) {
      for (const [rel, why] of Object.entries(rows)) {
        if (!present.has(rel)) missing.push(`${table}: ${rel} no longer exists`)
        if (!why || why.length < 20) missing.push(`${table}: ${rel} has no reason`)
      }
    }
    expect(missing).toEqual([])
  })

  it('spends the retired spellings nowhere', () => {
    /* The verbs makeStore replaced. Read over the whole tree, tests included:
       a case that still asks for getState is a call site the rename missed. */
    const RETIRED = /\b(getState|_resetAppsForTests|_clearForTests)\b/
    const found = []
    for (const path of sources(SRC, true)) {
      if (RETIRED.test(readFileSync(path, 'utf8'))) found.push(relPath(SRC, path))
    }
    expect(found, 'read a snapshot with get(); the test seam is _resetForTests').toEqual([])
  })
})
