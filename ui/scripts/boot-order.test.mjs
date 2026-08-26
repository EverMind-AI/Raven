/* The assembled page defers its first data-driven paint until live sources exist. */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const demo = readFileSync(new URL('../src/demo/160-boot.js', import.meta.url), 'utf8')
const build = readFileSync(new URL('../build.py', import.meta.url), 'utf8')
const manifest = build.match(/_LIVE_PARTS = \[(.*?)\n\]/s)
if (!manifest) throw new Error('_LIVE_PARTS is absent from build.py')
const live = [...manifest[1].matchAll(/"([^"]+\.js)"/g)]
  .map((m) => readFileSync(new URL(`../src/live/${m[1]}`, import.meta.url), 'utf8'))
  .join('')

const STEPS = [
  'lookLoad', 'paneLoad', 'setRail', 'sessionDraw', 'sessionOpen', 'drawCapsBadge',
  'drawPerm', 'drawCtx', 'drawCaps', 'drawFoot', 'bumpWs', 'drawSettings',
  'setRuntime', 'goState',
]

function harness(rows = []) {
  const calls = []
  const queued = []
  const page = {}
  const values = STEPS.map((name) => name === 'sessionRows'
    ? () => rows
    : (...args) => calls.push([name, ...args]))
  const install = Function(
    ...STEPS,
    'sessionRows', 'bootError', 'queueMicrotask', 'window', 'RavenIslands', 'DS', 'addEventListener',
    `${demo}\nreturn { bootPage };`,
  )
  const api = install(
    ...values,
    () => rows,
    (where, error) => { throw new Error(`${where}: ${error}`) },
    (fn) => queued.push(fn),
    page,
    { onboard: { open: () => {} } },
    {},
    () => {},
  )
  return { api, calls, page, queued }
}

describe('the assembled page boot order', () => {
  it('boots fixture mode after the assembled script task', () => {
    const row = { id: 'fixture' }
    const { calls, queued } = harness([row])
    expect(calls).toEqual([])
    expect(queued).toHaveLength(1)

    queued.shift()()

    expect(calls.map(([name]) => name)).toEqual(STEPS)
    expect(calls.find(([name]) => name === 'sessionOpen')).toEqual(['sessionOpen', row])
  })

  it('lets live claim the first paint and tolerates its empty session source', () => {
    const { api, calls, page, queued } = harness()
    page.__liveBoot = 1
    queued.shift()()
    expect(calls).toEqual([])

    queued.push(api.bootPage)
    queued.shift()()

    expect(calls.map(([name]) => name)).toEqual(STEPS.filter((name) => name !== 'sessionOpen'))
  })

  it('queues live boot only after every synchronous source installer', () => {
    const queuedAt = live.lastIndexOf('queueMicrotask(bootPage);')
    const installs = [...live.matchAll(/^DS\.[A-Za-z0-9_.]+\s*=/gm)].map((m) => m.index)
    expect(queuedAt).toBeGreaterThan(0)
    expect(installs.length).toBeGreaterThan(10)
    expect(installs.every((at) => at < queuedAt)).toBe(true)
    expect(live.slice(queuedAt).trim()).toBe('queueMicrotask(bootPage);\n\n})();')
    expect(live).not.toContain('CRONS.length = 0')
  })
})

/* Where the live layer starts recording which conversation the tab is on.
   An ordering rule no unit test can hold: the demo shell has already opened its
   canned session by the time this file runs, and the line above it clears the
   pointer again. Watching before either of those wrote `a` into the note and
   then deleted it, so a reload never had a conversation to come back to. */
describe('the live boot guard', () => {
  const guard = readFileSync(new URL('../src/live/010-boot-guard.js', import.meta.url), 'utf8')

  it('starts the view watch, and only after it has cleared the pointer', () => {
    const clear = guard.indexOf('sessionSet(null)')
    const watch = guard.indexOf('RavenIslands.view.watch()')
    expect(clear).toBeGreaterThan(-1)
    expect(watch).toBeGreaterThan(clear)
  })
})
