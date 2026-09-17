// @vitest-environment happy-dom
/* The page defers its first data-driven paint until every source is installed. */

import { readFileSync } from 'node:fs'
/* Off cwd, not off `import.meta.url`: under happy-dom that is an http URL. */
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { loadPart, moduleText } from './module-harness.mjs'

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

/* The boot module with every step of its own list replaced: what this is about
   is the order of the list and when it runs, neither of which any step's own
   work decides. */
async function harness(rows = []) {
  const calls = []
  const step = (name) => (...args) => calls.push([name, ...args])
  const part = await loadPart(() => import('../src/state/boot'), {
    fakes: {
      'src/state/look': { load: step('lookLoad') },
      'src/chrome/behaviour/panes': { load: step('paneLoad') },
      'src/state/perm': { draw: step('drawPerm') },
      'src/state/tier': { load: step('loadTier') },
      'src/state/ctxChip': { draw: step('drawCtx') },
      'src/state/foot': { draw: step('drawFoot') },
      'src/state/failureBar': {
        bootError: (where, error) => { throw new Error(`${where}: ${error}`) },
      },
      'src/state/rail': { set: step('setRail') },
      'src/state/envChip': { setRuntime: step('setRuntime') },
      'src/state/session/rows': { open: step('sessionOpen'), rows: () => rows },
      'src/state/caps': { draw: step('drawCaps') },
      'src/state/ws': { bump: step('bumpWs') },
    },
    islands: {
      onboard: { open: () => {} },
      rail: { draw: step('sessionDraw') },
      settings: { redraw: step('drawSettings') },
      composer: { goPaint: step('goState') },
    },
  })
  return { part, calls }
}

describe('the page boot order', () => {
  it('draws the first frame from what the page already holds, in order', async () => {
    const row = { id: 'fixture' }
    const { part, calls } = await harness([row])
    expect(calls).toEqual([])

    part.bootPage()

    expect(calls.map(([name]) => name)).toEqual(STEPS)
    expect(calls.find(([name]) => name === 'sessionOpen')).toEqual(['sessionOpen', row])
  })

  /* A live boot starts with an empty source and chooses a draft after the real
     list lands, so the one step with nothing to act on is skipped and the rest
     still run. */
  it('tolerates an empty session source', async () => {
    const { part, calls } = await harness()
    part.bootPage()
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

  /* The chrome installs first, in every mode. A page opened from disk or with
     ?stub=1 used to skip the live half and let the offline fixtures answer
     instead; the fixtures are responders behind the transport now
     (src/rpc/fixtures/), so one set of installers runs and the URL only decides
     which transport they read (src/state/transport.ts).

     The order between them is what the concatenated script's manifest was, and
     these four are the ones that depend on it: the palette's half of the
     composer source before the settings seam adds its own member to the same
     object, and both before the boot, which every one of them is read by. */
  it('installs the page chrome before the boot, in the manifest order', () => {
    const main = moduleText('main.tsx')
    const at = (needle) => {
      const ix = main.indexOf(needle)
      expect(ix, needle).toBeGreaterThan(-1)
      return ix
    }
    expect(at('installComposerPalette()')).toBeLessThan(at('langEffects.install()'))
    expect(at('langEffects.install()')).toBeLessThan(at('settingsChrome.install()'))
    expect(at('settingsChrome.install()')).toBeLessThan(at('boot()'))

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
