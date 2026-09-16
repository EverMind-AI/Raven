// @vitest-environment happy-dom
/* Which of `session.delete`'s three answers may drop the row.
 *
 * A null `deleted` carries two opposite meanings and the rail must act on them
 * differently, so the branch that tells them apart is pinned here: the only
 * other thing that reads `still_on_disk` is a browser.
 */

import { describe, expect, it, vi } from 'vitest'

import { fakeGateway, loadPart, looseQuery } from '../../../scripts/legacy-part.mjs'

interface Answer {
  deleted?: string | null
  still_on_disk?: boolean
}

interface Row { id: string; title: string }

/* What the parts install onto the seam. `DS` is a bare `{}` in the legacy
   source, so the shape a test drives it through is declared here. */
interface Seam {
  composer: object
  transcript: object
  sessions: { remove(s: Row): void; deleteAll(): Promise<void> }
}

const label = (key: string, vars?: Record<string, unknown>) =>
  (vars ? `${key}|${JSON.stringify(vars)}` : key)

/* `DS.sessions.remove` as live/080-overrides.js installs it. The rest of that
   part reaches for dozens of collaborators that have nothing to do with the
   decision under test, so those are fakes; the decision is the part's own.
   `RavenIslands.rail.removeRow` is what "the row goes" means -- nothing else
   in leaveDeletedSession is visible from outside it. */
async function harness(answer: Answer | Error) {
  const left = vi.fn()
  const toast = vi.fn()
  /* `remove` does not return the confirm callback's promise, so the harness
     holds it: without it the assertions run before the toast is written. */
  let settled: Promise<void> = Promise.resolve()
  const part = await loadPart(() => import('../../legacy/live/080-overrides.js'), {
    fakes: {
      'demo/010-kernel.js': { $: looseQuery(), T: label },
      'demo/040-state.js': {
        confirmAsk: (_t: string, _b: string, _l: string, run: () => Promise<void>) => {
          settled = run()
        },
        dropDraft: () => {},
        sheetsForget: () => {},
      },
      'demo/050-rail.js': {
        sessionDraw: () => {},
        sessionReplace: () => {},
        sessionRows: () => [],
      },
    },
    globals: {
      sessionCurrent: () => null,
      toast,
      RavenIslands: {
        dag: { forget: () => {} },
        rail: {
          endRename: () => {},
          removeRow: (rows: Row[], _current: string | null, id: string) => {
            left(id)
            return { kind: 'unchanged', rows }
          },
        },
      },
    },
  })
  await fakeGateway(async () => {
    if (answer instanceof Error) throw answer
    return answer
  })
  const { DS } = await import('../../legacy/seam/000-datasource.js')
  const ds = DS as unknown as Seam
  ds.composer = {}
  ds.sessions = {} as Seam['sessions']
  ds.transcript = {}
  part.install()
  return {
    run: async (s: Row) => { ds.sessions.remove(s); await settled },
    left,
    toast,
  }
}

const row = { id: 'tui:20260610_100000_a1', title: 'a deck' }

describe('deleting a session from the rail', () => {
  it('drops the row when the file was removed', async () => {
    const { run, left, toast } = await harness({ deleted: row.id, still_on_disk: false })
    await run(row)
    expect(left).toHaveBeenCalledWith(row.id)
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.deleted_x'))
  })

  it('drops the row when there was nothing to remove', async () => {
    /* The case that could not be cleared at all before: a conversation whose
       first turn never saved a file answers a null `deleted`, and the reader's
       goal -- the row gone -- already holds. */
    const { run, left, toast } = await harness({ deleted: null, still_on_disk: false })
    await run(row)
    expect(left).toHaveBeenCalledWith(row.id)
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.delete_absent'))
  })

  it('keeps the row when the file survived its removal', async () => {
    const { run, left, toast } = await harness({ deleted: null, still_on_disk: true })
    await run(row)
    expect(left).not.toHaveBeenCalled()
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.delete_failed'))
  })

  it('keeps the row when the server does not carry the field', async () => {
    /* A page rebuilt against a still-running older engine: saying nothing is not
       saying "nothing was there", so the pre-field behaviour stands. */
    const { run, left, toast } = await harness({ deleted: null })
    await run(row)
    expect(left).not.toHaveBeenCalled()
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.delete_failed'))
  })

  it('reports a transport failure without dropping the row', async () => {
    const { run, left, toast } = await harness(new Error('not connected'))
    await run(row)
    expect(left).not.toHaveBeenCalled()
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.delete_failed'))
  })
})

/* The bulk path reaches for the rail's own helpers; each is a fake so the test
   can read which rows it decided were gone. */
async function bulkHarness(answers: Record<string, Answer | Error>) {
  let live = Object.keys(answers).map((id) => ({ id, title: id }))
  const part = await loadPart(() => import('../../legacy/live/130-writes.js'), {
    fakes: {
      'demo/010-kernel.js': { $: looseQuery(), T: label },
      'demo/040-state.js': { dropDraft: () => {} },
      'demo/050-rail.js': {
        sessionReplace: (next: Row[]) => { live = next },
        sessionRows: () => live,
      },
      'demo/130-settings.js': { drawSettings: () => {} },
      'live/080-overrides.js': { startDraft: () => {} },
    },
    globals: { sessionCurrent: () => null, sessionSet: () => {}, toast: () => {} },
  })
  await fakeGateway(async (_method: string, p: { session_id: string }) => {
    const a = answers[p.session_id]
    if (a instanceof Error) throw a
    return a
  })
  const { DS } = await import('../../legacy/seam/000-datasource.js')
  const ds = DS as unknown as Seam
  ds.sessions = {} as Seam['sessions']
  part.install()
  return { deleteAll: () => ds.sessions.deleteAll(), left: () => live.map((s) => s.id) }
}

describe('clearing every session from the rail', () => {
  it('keeps the rows whose files survived their removal', async () => {
    /* The reported break: a refusal is a resolved promise, so awaiting it
       without reading the answer counted it as a removal. The row vanished and
       came back on the next reload. */
    const { deleteAll, left } = await bulkHarness({
      gone: { deleted: 'gone', still_on_disk: false },
      kept: { deleted: null, still_on_disk: true },
    })
    await deleteAll()
    expect(left()).toEqual(['kept'])
  })

  it('drops a row that had no file to remove', async () => {
    /* Same split the single delete makes: nothing left to remove is the
       reader's own goal, so the row goes. */
    const { deleteAll, left } = await bulkHarness({ lazy: { deleted: null, still_on_disk: false } })
    await deleteAll()
    expect(left()).toEqual([])
  })

  it('keeps a row when the server does not carry the field', async () => {
    const { deleteAll, left } = await bulkHarness({ old: { deleted: null } })
    await deleteAll()
    expect(left()).toEqual(['old'])
  })

  it('keeps a row the transport never answered for', async () => {
    const { deleteAll, left } = await bulkHarness({ down: new Error('not connected') })
    await deleteAll()
    expect(left()).toEqual(['down'])
  })
})
