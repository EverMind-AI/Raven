/* What the live layer hands the sheet when a graph starts.
 *
 * Three lines of wiring with no island behind them, so the vitest suite cannot
 * reach it -- the same blind spot that let the agent roster filter the wrong
 * field. The run the sheet is titled by is built here, and a field this object
 * does not carry is a field the sheet cannot draw. */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

const build = readFileSync(resolve(process.cwd(), 'build.py'), 'utf8')
const manifest = build.match(/_LIVE_PARTS = \[(.*?)\n\]/s)
if (!manifest) throw new Error('_LIVE_PARTS is absent from build.py')
const live = [...manifest[1].matchAll(/"([^"]+\.js)"/g)]
  .map((m) => readFileSync(resolve(process.cwd(), 'src/live', m[1]), 'utf8'))
  .join('')

/* The object literal the sheet is started with, lifted out and evaluated
   against a payload -- not read as text, so a field that is present but wired
   to the wrong thing fails too. */
function startedRun(payload) {
  const mark = 'RavenIslands.dag.start(key, {'
  const at = live.indexOf(mark)
  if (at < 0) throw new Error('the sheet is not started from the assembled live layer')
  const open = live.indexOf('{', at)
  let depth = 0
  let close = -1
  for (let i = open; i < live.length; i += 1) {
    if (live[i] === '{') depth += 1
    else if (live[i] === '}') { depth -= 1; if (depth === 0) { close = i; break } }
  }
  const literal = live.slice(open, close + 1)
  return Function('p', 'key', 'started', `return (${literal});`)(payload, 'sess-1', [])
}

describe('the run the live layer starts the sheet with', () => {
  it('carries the line the graph was dispatched with', () => {
    const run = startedRun({ run_id: 'r1', task_summary: 'AI news pipeline', nodes: [] })
    expect(run.task_summary).toBe('AI news pipeline')
    expect(run.run_id).toBe('r1')
  })

  it('carries null, not undefined, for a run started before the field existed', () => {
    /* `DagRun.task_summary` is `string | null`, and the sheet tests the value
       rather than its presence. */
    expect(startedRun({ run_id: 'r1', nodes: [] }).task_summary).toBeNull()
  })
})
