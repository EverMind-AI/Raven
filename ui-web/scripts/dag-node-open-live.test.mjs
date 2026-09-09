/* Opening a graph node raises that node's window and nothing else.
 *
 * The two openers live in the live layer and are only reachable through the
 * assembled bundle, so they are sliced out of it the way the other live-path
 * tests here do. What they must NOT do is the point: `wsPick`/`setWs` route to
 * `openDeskTab` in desk mode, whose whole job is to open the little desk, so a
 * node opened from the trail's card or from the sheet popped the palette beside
 * the window the reader had actually asked for.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const build = readFileSync(new URL('../build.py', import.meta.url), 'utf8')
const manifest = build.match(/_LIVE_PARTS = \[(.*?)\n\]/s)
if (!manifest) throw new Error('_LIVE_PARTS is absent from build.py')
const parts = [...manifest[1].matchAll(/"([^"]+\.js)"/g)].map((m) => m[1])
const live = parts
  .map((name) => readFileSync(new URL(`../src/live/${name}`, import.meta.url), 'utf8'))
  .join('')

/* The source of one brace-balanced declaration, from its opening marker. */
function block(mark) {
  const start = live.indexOf(mark)
  if (start < 0) throw new Error(`${mark} is absent from the assembled live layer`)
  const brace = live.indexOf('{', start)
  let depth = 0
  for (let index = brace; index < live.length; index += 1) {
    if (live[index] === '{') depth += 1
    else if (live[index] === '}') {
      depth -= 1
      if (depth === 0) return live.slice(start, index + 1)
    }
  }
  throw new Error(`${mark} has no closing brace in the assembled live layer`)
}

/* The class the shell sets while the floating desk owns the workspace. */
function deskReady(on) {
  const list = new Set(on ? ['desk-ready'] : [])
  globalThis.document = { documentElement: { classList: { contains: (name) => list.has(name) } } }
}

function harness(source, exportName, { rows = [{ kind: 'spawn', agent: 'raven', label: 'qc' }] } = {}) {
  const calls = []
  const install = Function(
    'RavenIslands', 'DS', 'wsOpen', 'setWs', 'wsPick', 'drawWs', 'plainTitle', 'setTimeout',
    `${source}\nreturn ${exportName};`,
  )
  const fn = install(
    {
      subagents: {
        openDagNode: (run, node) => calls.push(['openDagNode', run, node.id]),
        rows: () => rows,
        openRow: (row) => calls.push(['openRow', row.label]),
        refresh: () => calls.push(['refresh']),
      },
      workspace: { openDeskTab: (tab) => calls.push(['openDeskTab', tab]) },
    },
    { transcript: {} },
    false,
    (open, tab) => calls.push(['setWs', open, tab ?? null]),
    (tab) => calls.push(['wsPick', tab]),
    () => calls.push(['drawWs']),
    (s) => s,
    /* Retries run straight through, so the give-up branch is reachable without
       a clock. */
    (fn2) => fn2(),
  )
  return { fn, calls }
}

describe('opening a graph node from the live layer', () => {
  it('raises the node window alone while the floating desk is up', () => {
    deskReady(true)
    const { fn, calls } = harness(block('function dagOpenNode(runId, n) {'), 'dagOpenNode')

    fn('r1', { id: 'brief' })

    expect(calls).toEqual([['openDagNode', 'r1', 'brief']])
    /* Named explicitly, because these are what popped the palette: either one
       reaches `openDeskTab` in desk mode. */
    expect(calls.some(([verb]) => verb === 'wsPick' || verb === 'setWs')).toBe(false)
  })

  it('still selects the panel view where there are no windows', () => {
    /* The pre-desk panel, where picking the agents view was how the instance
       got on screen at all. */
    deskReady(false)
    const { fn, calls } = harness(block('function dagOpenNode(runId, n) {'), 'dagOpenNode')

    fn('r1', { id: 'brief' })

    expect(calls).toEqual([
      ['openDagNode', 'r1', 'brief'],
      ['setWs', true, null],
      ['wsPick', 'agents'],
      ['drawWs'],
    ])
  })

  it('opens a spawn record without the palette either', () => {
    deskReady(true)
    const source = block('DS.transcript.openSpawn = (agent, label) => {')
    const { fn, calls } = harness(`${source};`, 'DS.transcript.openSpawn')

    fn('raven', 'qc')

    expect(calls).toEqual([['openRow', 'qc']])
  })

  it('falls back to the agents list when no row ever turns up', () => {
    /* A click that opens nothing reads as broken, and `refresh` keeps the drawn
       list on a failed read rather than emptying it -- so a gateway hiccup or a
       label the registry spells differently lands here. The list is somewhere
       to look; the window this MR stops opening a palette beside was never
       raised on this branch. */
    deskReady(true)
    const source = block('DS.transcript.openSpawn = (agent, label) => {')
    const { fn, calls } = harness(`${source};`, 'DS.transcript.openSpawn', { rows: [] })

    fn('raven', 'qc')

    expect(calls.filter(([verb]) => verb === 'openRow')).toEqual([])
    expect(calls[calls.length - 1]).toEqual(['openDeskTab', 'agents'])
  })

  it('keeps the panel view for a spawn record without the desk', () => {
    deskReady(false)
    const source = block('DS.transcript.openSpawn = (agent, label) => {')
    const { fn, calls } = harness(`${source};`, 'DS.transcript.openSpawn')

    fn('raven', 'qc')

    expect(calls).toEqual([['setWs', true, 'agents'], ['openRow', 'qc']])
  })
})
