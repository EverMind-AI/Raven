// @vitest-environment happy-dom
/* The page defers its first data-driven paint until every source is installed.
 *
 * One concern per gate: the first-run model redirect and the permission chip
 * used to ride along in here, over a concatenation of eight modules, and are
 * their own gates now (first-run-model-setup.test.mjs, permission-chip.test.mjs)
 * over the modules that actually carry them. What is left is the sequence: the
 * order boot() runs its steps in, the order bootPage() paints in, and the two
 * facts about the claim on the first frame that no rendered tree can show.
 *
 * Driven rather than read where a run can show it. `boot()`'s three steps used
 * to be read off its body with a regex anchored to two-space indentation, which
 * made a reformat a failure and a reordering inside a nested block invisible;
 * the steps are recorded through fakes now, the way the paint below always was.
 */

import { describe, expect, it } from 'vitest'

import { loadPart, moduleText } from '../module-harness.mjs'

const bootText = moduleText('app/boot.ts')
const installText = moduleText('app/install.ts')

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
   work decides.
 *
 * The three fakes beyond the paint's own -- the page's install, the rail's hold
 * and the socket -- are what make `boot()` drivable: its three steps are module
 * -private functions, and each one's first visible act is a call into one of
 * them (claimFirstFrame holds the rail, sequence asks the socket to connect).
 * `connect` returns a promise nothing resolves, so the sequence parks at its
 * first await and the rest of it cannot interleave with what is being measured.
 */
async function harness(rows = []) {
  const calls = []
  const step = (name) => (...args) => calls.push([name, ...args])
  const part = await loadPart(() => import('../../src/app/boot'), {
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
      'src/state/session/resume': { watch: step('watchNote'), landing: () => null },
      'src/app/install': { installPage: step('installPage') },
      'src/rpc/gateway': {
        gateway: () => ({
          connect: () => { calls.push(['connect']); return new Promise(() => {}) },
        }),
      },
      'src/features/onboard/store': {
        open: () => {},
      },
      'src/features/rail/store': {
        draw: step('sessionDraw'),
        hold: step('holdRail'),
        release: step('releaseRail'),
      },
      'src/features/settings/store': {
        redraw: step('drawSettings'),
      },
      'src/features/composer/mount': {
        goPaint: step('goState'),
      },
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

  /* "Before" is the order boot() runs its three steps in, and the queue being
     the last of them: the seam has to answer before the rail is held (the hold
     paints), every source has to be installed before the first data-driven
     paint is queued, and the paint is queued rather than called for exactly
     that reason. */
  it('claims the frame, installs the page and opens the socket, then queues the paint', async () => {
    const { part, calls } = await harness()

    part.boot()

    /* The three steps, in order -- and not one step of the paint, because it
       was queued rather than run. */
    expect(calls.map(([name]) => name)).toEqual(['holdRail', 'watchNote', 'installPage', 'connect'])

    /* The microtask boot() queued runs before this one, which is the whole of
       "queued after every synchronous installer". */
    await Promise.resolve()

    expect(calls.map(([name]) => name))
      .toEqual(['holdRail', 'watchNote', 'installPage', 'connect', ...STEPS.filter((n) => n !== 'sessionOpen')])
  })

  /* And the queue is written in one place. Two modules could each queue a
     paint, and the page would draw twice from a seam that was whole only once.
   */
  it('queues the first paint from the boot module alone', () => {
    const carriers = [
      ['app/boot.ts', bootText],
      ['app/install.ts', installText],
    ].filter(([, text]) => text.includes('queueMicrotask(bootPage)'))
    expect(carriers.map(([name]) => name)).toEqual(['app/boot.ts'])

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
     which transport they read (src/rpc/chooseTransport.ts).

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
    const watch = bootText.indexOf('watchSessionNote()')
    expect(clear).toBeGreaterThan(-1)
    expect(watch).toBeGreaterThan(clear)
  })

  /* Both of those paint, and both read the session source, so it has to answer
     before either runs. */
  it('installs the session source before it holds the rail', () => {
    const install = bootText.indexOf('sources.rail = sessionsSource')
    expect(install).toBeGreaterThan(-1)
    expect(bootText.indexOf('holdRail()')).toBeGreaterThan(install)
  })
})
