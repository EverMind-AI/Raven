// @vitest-environment happy-dom
/* The page defers its first data-driven paint until every source is installed. */

import { readFileSync } from 'node:fs'
/* Off cwd, not off `import.meta.url`: under happy-dom that is an http URL. */
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { loadPart, moduleText } from './legacy-part.mjs'

const index = readFileSync(resolve(process.cwd(), 'src/legacy/index.js'), 'utf8')
const bootText = moduleText('state/boot.ts')
const installText = moduleText('state/install.ts')
/* The page's own half, as one text: the claim on the first frame, the wiring,
   the gateway sequence and the settings seam were the whole of the live layer
   and are six modules now, and every rule below is about the whole of it rather
   than about which of them a line sits in. */
const wiring = bootText + installText + moduleText('state/connection.ts')
  + moduleText('state/updates.ts') + moduleText('state/langPick.ts')
  + moduleText('state/langEffects.ts') + moduleText('features/settings/chrome.ts')
  + moduleText('features/model/chip.ts')

/* One step fewer than the concatenated boot had: `drawCapsBadge` was an empty
   function -- the rail's module rows carry no counters -- and it went with the
   rest of the draw shells. */
const STEPS = [
  'lookLoad', 'paneLoad', 'setRail', 'sessionDraw', 'sessionOpen',
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
      'src/state/rail': { set: step('setRail') },
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
      'demo/130-settings.js': { drawSettings: step('drawSettings'), setRuntime: step('setRuntime') },
      'src/state/caps': { draw: step('drawCaps') },
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
    /* What the boot's claim on the first frame does on its way in: the demo
       boot backs off and the page decides when the splash lifts. */
    part.claimBoot()
    queued.shift()()
    expect(calls).toEqual([])

    queued.push(part.bootPage)
    queued.shift()()

    expect(calls.map(([name]) => name)).toEqual(STEPS.filter((name) => name !== 'sessionOpen'))
  })

  /* "Before" is not a position in one concatenated text any more: it is the
     order `boot()` calls its three steps in, and the queue being the last of
     them. */
  it('queues the first paint only after every synchronous source installer', () => {
    const carriers = [
      ['state/boot.ts', bootText],
      ['state/install.ts', installText],
    ].filter(([, text]) => text.includes('queueMicrotask(bootPage)'))
    expect(carriers.map(([name]) => name)).toEqual(['state/boot.ts'])

    const opened = bootText.indexOf('export function boot(): void {')
    const body = bootText.slice(opened, bootText.indexOf('\n}\n', opened))
    /* The three steps in order, and the queue after all of them. */
    expect([...body.matchAll(/^ {2}(?:void )?(\w+)\(/gm)].map((m) => m[1]))
      .toEqual(['claimFirstFrame', 'installPage', 'sequence', 'queueMicrotask'])
    expect(body.trimEnd().endsWith('queueMicrotask(bootPage)')).toBe(true)

    const installs = [...installText.matchAll(/^\s*sources\.[A-Za-z0-9_.]+\s*=/gm)]
    expect(installs.length).toBeGreaterThan(10)
    /* installPage() is what boot() calls, so every list has to be in it. */
    const lists = installText.slice(installText.indexOf('export function installPage(): void {'))
    for (const step of ['installSources', 'installPushes', 'installActions', 'installDevHooks']) {
      expect(lists).toContain(`${step}()`)
    }
  })

  /* The chrome installs first, in every mode. The live layer used to be
     skipped for a page opened from disk or with ?stub=1, where the demo
     half's fixture sources answered instead; the fixtures are responders
     behind the transport now (src/rpc/fixtures/), so one set of parts
     installs and the URL only decides which transport they read
     (src/state/transport.ts). The live layer itself is gone: its last part
     is state/lang{Pick,Effects}.ts and features/settings/chrome.ts, which
     main.tsx installs at the point that part installed. */
  it('installs the legacy chrome before the boot, and the settings seam after it', () => {
    const listed = [...index.matchAll(/^import \* as \w+ from '\.\/live\/([^']+)'$/gm)].map((m) => m[1])
    expect(listed).toEqual([])
    expect(index).toContain('for (const part of DEMO) part.install()')

    const main = moduleText('main.tsx')
    expect(main.indexOf('installLegacy()')).toBeLessThan(main.indexOf('langEffects.install()'))
    expect(main.indexOf('langEffects.install()')).toBeLessThan(main.indexOf('settingsChrome.install()'))
    expect(main.indexOf('settingsChrome.install()')).toBeLessThan(main.indexOf('boot()'))

    expect(wiring).not.toContain('CRONS.length = 0')
  })
})

describe('first-run model setup', () => {
  it('records missing-provider state without opening onboarding automatically', () => {
    expect(wiring).toContain('setupState.providerConfigured = setup.provider_configured !== false')
    expect(wiring).not.toMatch(/setup\.provider_configured === false\s*\|\|/)
  })

  it('guards New Task, Send, and the model selector with the same redirect', () => {
    expect(wiring).toContain('.beforeSend = openModelsForMissingProvider')
    expect(wiring.match(/if \(openModelsForMissingProvider\(\)\) return/g)).toHaveLength(2)
  })
})

/* Where the page starts recording which conversation the tab is on.
   An ordering rule no unit test can hold: the demo chrome has already opened its
   canned session by the time the boot runs, and the line above it clears
   the pointer again. Watching before either of those wrote `a` into the note and
   then deleted it, so a reload never had a conversation to come back to. */
describe('the claim on the first frame', () => {
  it('starts the view watch, and only after it has cleared the pointer', () => {
    const clear = bootText.indexOf('sessionSet(null)')
    const watch = bootText.indexOf('islands.view.watch()')
    expect(clear).toBeGreaterThan(-1)
    expect(watch).toBeGreaterThan(clear)
  })

  /* Both of those paint, and both read the session source, so it has to answer
     before either runs. */
  it('installs the session source before it holds the rail', () => {
    const install = bootText.indexOf('sources.sessions = sessionsSource')
    expect(install).toBeGreaterThan(-1)
    expect(bootText.indexOf('islands.rail.hold()')).toBeGreaterThan(install)
  })
})

describe('the permission chip mirrors every settings load', () => {
  /* The push lives outside loadSettings, so each caller must invoke it itself
     -- boot included, or a cold page shows the localStorage cache while the
     gate enforces the server's mode. Two of the three callers are the settings
     source's own verbs now, so its text is read beside the layer's. */
  const settingsSource = readFileSync(resolve(process.cwd(), 'src/features/settings/source.ts'), 'utf8')

  it('each loadSettings call site pushes the mode afterwards', () => {
    const text = wiring + settingsSource
    const callers = [...text.matchAll(/(?<!function )loadSettings\(\)/g)].length
    const pushes = [...text.matchAll(/pushPermMode\(\)|\.then\(pushPermMode\)/g)].length
    expect(callers).toBeGreaterThanOrEqual(3)
    expect(pushes).toBe(callers)
  })
})
