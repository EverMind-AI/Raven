// @vitest-environment happy-dom
/* What the live layer hands the sheet when a graph starts.
 *
 * Three lines of wiring with no island behind them, so the vitest suite cannot
 * reach it -- the same blind spot that let the agent roster filter the wrong
 * field. The run the sheet is titled by is built here, and a field this object
 * does not carry is a field the sheet cannot draw. */

import { describe, expect, it } from 'vitest'

import { loadPart, looseQuery } from './legacy-part.mjs'

/* Driven through the real dispatcher rather than by lifting the object literal
   out as text: a field that is present but wired to the wrong thing fails too. */
async function startedRun(payload) {
  const started = []
  const part = await loadPart(() => import('../src/legacy/live/050-turn.js'), {
    fakes: {
      'demo/010-kernel.js': { $: looseQuery() },
      'demo/040-state.js': { sheetSession: () => 'sess-1' },
      'demo/070-transcript.js': { dagFlowFeed: () => {} },
    },
    globals: {
      RavenIslands: {
        dag: {
          fromStarted: () => [],
          start: (key, run) => started.push({ key, run }),
        },
      },
      sessionCurrent: () => 'sess-1',
    },
  })
  part.onEvent({ type: 'dag.run_started', payload })
  if (!started.length) throw new Error('the sheet was not started from the live layer')
  return started[0].run
}

describe('the run the live layer starts the sheet with', () => {
  it('carries the line the graph was dispatched with', async () => {
    const run = await startedRun({ run_id: 'r1', task_summary: 'AI news pipeline', nodes: [] })
    expect(run.task_summary).toBe('AI news pipeline')
    expect(run.run_id).toBe('r1')
  })

  it('carries null, not undefined, for a run started before the field existed', async () => {
    /* `DagRun.task_summary` is `string | null`, and the sheet tests the value
       rather than its presence. */
    expect((await startedRun({ run_id: 'r1', nodes: [] })).task_summary).toBeNull()
  })
})
