// @vitest-environment happy-dom
/* The page defers its first data-driven paint until live sources exist. */

import { readFileSync } from 'node:fs'
/* Off cwd, not off `import.meta.url`: under happy-dom that is an http URL. */
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { loadPart, partNames, partTexts } from './legacy-part.mjs'

const liveParts = partNames('live')
const liveTexts = partTexts('live').map(([, text]) => text)
const live = liveTexts.join('')
const index = readFileSync(resolve(process.cwd(), 'src/legacy/index.js'), 'utf8')

const STEPS = [
  'lookLoad', 'paneLoad', 'setRail', 'sessionDraw', 'sessionOpen', 'drawCapsBadge',
  'drawPerm', 'loadTier', 'drawCtx', 'drawCaps', 'drawFoot', 'bumpWs', 'drawSettings',
  'setRuntime', 'goState',
]

/* The boot part installed with every step of its own list replaced, and the
   microtask queue held: what this is about is the order of the list and when
   it runs, neither of which any step's own work decides. */
async function harness(rows = []) {
  const calls = []
  const queued = []
  const step = (name) => (...args) => calls.push([name, ...args])
  const part = await loadPart(() => import('../src/legacy/demo/160-boot.js'), {
    fakes: {
      'src/shell/look': { load: step('lookLoad') },
      'src/shell/panes': { load: step('paneLoad') },
      'src/shell/perm': { draw: step('drawPerm') },
      'src/shell/tier': { load: step('loadTier') },
      'src/shell/ctxchip': { draw: step('drawCtx') },
      'src/shell/foot': { draw: step('drawFoot') },
      'demo/040-state.js': {
        bootError: (where, error) => { throw new Error(`${where}: ${error}`) },
      },
      'demo/050-rail.js': {
        sessionDraw: step('sessionDraw'),
        sessionOpen: step('sessionOpen'),
        sessionRows: () => rows,
      },
      'demo/090-composer.js': { goState: step('goState') },
      'demo/100-workspace.js': { bumpWs: step('bumpWs') },
      'demo/120-capabilities.js': { drawCapsBadge: step('drawCapsBadge') },
      'demo/130-settings.js': { drawSettings: step('drawSettings'), setRuntime: step('setRuntime') },
      'demo/150-chrome.js': { setRail: step('setRail') },
      'demo/152-skills.js': { drawCaps: step('drawCaps') },
    },
    islands: { onboard: { open: () => {} } },
  })
  /* Held rather than let run: the whole claim is that the queued boot happens
     after the install pass, so the test has to be the one that releases it. */
  const real = globalThis.queueMicrotask
  globalThis.queueMicrotask = (fn) => queued.push(fn)
  try { part.install() } finally { globalThis.queueMicrotask = real }
  return { part, calls, queued }
}

describe('the page boot order', () => {
  it('boots fixture mode after the install pass', async () => {
    const row = { id: 'fixture' }
    const { calls, queued } = await harness([row])
    expect(calls).toEqual([])
    expect(queued).toHaveLength(1)

    queued.shift()()

    expect(calls.map(([name]) => name)).toEqual(STEPS)
    expect(calls.find(([name]) => name === 'sessionOpen')).toEqual(['sessionOpen', row])
  })

  it('lets live claim the first paint and tolerates its empty session source', async () => {
    const { part, calls, queued } = await harness()
    /* What live/010-boot-guard.js does on its way in: the demo boot backs off
       and the live half decides when the splash lifts. */
    part.claimBoot()
    queued.shift()()
    expect(calls).toEqual([])

    queued.push(part.bootPage)
    queued.shift()()

    expect(calls.map(([name]) => name)).toEqual(STEPS.filter((name) => name !== 'sessionOpen'))
  })

  /* The parts are modules, so "before" is not a position in one concatenated
     text: it is the order src/legacy/index.js installs them in. Three things
     carry the rule -- the queue is the last part's, it is the last thing that
     part installs, and the index installs the live parts in that order, after
     the demo half and only in live mode. */
  it('queues live boot only after every synchronous source installer', () => {
    const carriers = liveParts.filter((_, i) => liveTexts[i].includes('queueMicrotask(bootPage);'))
    expect(carriers).toEqual([liveParts.at(-1)])

    const last = liveTexts.at(-1)
    const opened = last.indexOf('export function install() {')
    const body = last.slice(opened, last.indexOf('\n}\n', opened))
    expect(body.trimEnd().endsWith('queueMicrotask(bootPage);')).toBe(true)

    const installs = [...live.matchAll(/^\s*sources\.[A-Za-z0-9_.]+\s*=/gm)]
    expect(installs.length).toBeGreaterThan(10)

    const listed = [...index.matchAll(/^import \* as \w+ from '\.\/live\/([^']+)'$/gm)].map((m) => m[1])
    expect(listed).toEqual(liveParts)
    expect(index).toContain('for (const part of DEMO) part.install()')
    expect(index).toContain('if (!liveMode()) return')
    expect(index.indexOf('for (const part of DEMO)')).toBeLessThan(index.indexOf('for (const part of LIVE)'))

    expect(live).not.toContain('CRONS.length = 0')
  })
})

describe('first-run model setup', () => {
  it('records missing-provider state without opening onboarding automatically', () => {
    expect(live).toContain('setupState.providerConfigured = setup.provider_configured !== false;')
    expect(live).not.toMatch(/setup\.provider_configured === false\s*\|\|/)
  })

  it('guards New Task, Send, and the model selector with the same redirect', () => {
    expect(live).toContain('sources.composer.beforeSend = openModelsForMissingProvider;')
    expect(live.match(/if \(openModelsForMissingProvider\(\)\) return;/g)).toHaveLength(2)
  })
})

/* Where the live layer starts recording which conversation the tab is on.
   An ordering rule no unit test can hold: the demo shell has already opened its
   canned session by the time this part installs, and the line above it clears
   the pointer again. Watching before either of those wrote `a` into the note and
   then deleted it, so a reload never had a conversation to come back to. */
describe('the live boot guard', () => {
  const guard = readFileSync(resolve(process.cwd(), 'src/legacy/live/010-boot-guard.js'), 'utf8')

  it('starts the view watch, and only after it has cleared the pointer', () => {
    const clear = guard.indexOf('sessionSet(null)')
    const watch = guard.indexOf('islands.view.watch()')
    expect(clear).toBeGreaterThan(-1)
    expect(watch).toBeGreaterThan(clear)
  })
})

describe('the permission chip mirrors every settings load', () => {
  /* The push lives outside loadSettings, so each caller must invoke it itself
     -- boot included, or a cold page shows the localStorage cache while the
     gate enforces the server's mode. */
  it('each loadSettings call site pushes the mode afterwards', () => {
    const callers = [...live.matchAll(/(?<!function )loadSettings\(\)/g)].length
    const pushes = [...live.matchAll(/pushPermMode\(\)|\.then\(pushPermMode\)/g)].length
    expect(callers).toBeGreaterThanOrEqual(3)
    expect(pushes).toBe(callers)
  })
})
