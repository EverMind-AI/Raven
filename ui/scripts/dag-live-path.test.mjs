/* The shipped live manifest keeps the delivered DAG opener on the island path. */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const build = readFileSync(new URL('../build.py', import.meta.url), 'utf8')
const manifest = build.match(/_LIVE_PARTS = \[(.*?)\n\]/s)
if (!manifest) throw new Error('_LIVE_PARTS is absent from build.py')
const parts = [...manifest[1].matchAll(/"([^"]+\.js)"/g)].map((m) => m[1])
const live = parts
  .map((name) => readFileSync(new URL(`../src/live/${name}`, import.meta.url), 'utf8'))
  .join('')

function openerAssignment() {
  const mark = 'DS.transcript.openDagRun = function (runId) {'
  const start = live.indexOf(mark)
  if (start < 0) throw new Error('openDagRun is absent from the assembled live layer')
  const brace = live.indexOf('{', start)
  let depth = 0
  for (let index = brace; index < live.length; index += 1) {
    if (live[index] === '{') depth += 1
    else if (live[index] === '}') {
      depth -= 1
      if (depth === 0) return live.slice(start, index + 2)
    }
  }
  throw new Error('openDagRun has no closing brace in the assembled live layer')
}

describe('the assembled live DAG opener', () => {
  it('opens the island run last node and keeps the agents fallback', () => {
    const calls = []
    const DS = { transcript: {} }
    const RavenIslands = {
      dag: { run: (key) => key === 'a' ? { run_id: 'r1', order: ['first', 'last'] } : null },
    }
    const install = Function(
      'DS', 'RavenIslands', 'sheetSession', 'dagOpenNode', 'setWs',
      `${openerAssignment()}\nreturn DS.transcript.openDagRun;`,
    )
    const open = install(
      DS,
      RavenIslands,
      () => 'a',
      (runId, node) => calls.push(['node', runId, node.id]),
      (...args) => calls.push(['fallback', ...args]),
    )

    open('r1')
    open('missing')

    expect(calls).toEqual([
      ['node', 'r1', 'last'],
      ['fallback', true, 'agents'],
    ])
  })
})
