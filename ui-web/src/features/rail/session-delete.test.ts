// @vitest-environment happy-dom
/* Which of `session.delete`'s three answers may drop the row.
 *
 * A null `deleted` carries two opposite meanings and the rail must act on them
 * differently, so the branch that tells them apart is pinned here: the live
 * layer is plain script, and the only other thing that reads `still_on_disk` is
 * a browser.
 */

// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'

import { describe, expect, it, vi } from 'vitest'

const source = readFileSync('src/live/080-overrides.js', 'utf8')

/* Just the one assignment, not the file: the rest of this layer reaches for
   dozens of globals that have nothing to do with the decision under test. */
const START = 'DS.sessions.remove = function (s) {'
const begin = source.indexOf(START)
const end = source.indexOf('\n};', begin)
const removeSource = source.slice(begin, end + 3)

interface Answer {
  deleted?: string | null
  still_on_disk?: boolean
}

function harness(answer: Answer | Error) {
  const DS = { sessions: {} as { remove(s: { id: string; title: string }): void } }
  const left = vi.fn(async () => {})
  const toast = vi.fn()
  const rpc = {
    call: vi.fn(async () => {
      if (answer instanceof Error) throw answer
      return answer
    }),
  }
  /* `remove` does not return the confirm callback's promise, so the harness
     holds it: without it the assertions run before the toast is written. */
  let settled: Promise<void> = Promise.resolve()
  const build = new Function(
    'DS', 'rpc', 'T', 'toast', 'confirmAsk', 'leaveDeletedSession',
    `${removeSource}\nreturn DS.sessions.remove;`,
  ) as (...args: unknown[]) => (s: { id: string; title: string }) => void
  const remove = build(
    DS, rpc,
    (key: string, vars?: Record<string, unknown>) => (vars ? `${key}|${JSON.stringify(vars)}` : key),
    toast,
    (_t: string, _b: string, _l: string, run: () => Promise<void>) => { settled = run() },
    left,
  )
  return { run: async (s: { id: string; title: string }) => { remove(s); await settled }, left, toast }
}

const row = { id: 'tui:20260610_100000_a1', title: 'a deck' }

describe('deleting a session from the rail', () => {
  it('drops the row when the file was removed', async () => {
    const { run, left, toast } = harness({ deleted: row.id, still_on_disk: false })
    await run(row)
    expect(left).toHaveBeenCalledWith(row.id)
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.deleted_x'))
  })

  it('drops the row when there was nothing to remove', async () => {
    /* The case that could not be cleared at all before: a conversation whose
       first turn never saved a file answers a null `deleted`, and the reader's
       goal -- the row gone -- already holds. */
    const { run, left, toast } = harness({ deleted: null, still_on_disk: false })
    await run(row)
    expect(left).toHaveBeenCalledWith(row.id)
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.delete_absent'))
  })

  it('keeps the row when the file survived its removal', async () => {
    const { run, left, toast } = harness({ deleted: null, still_on_disk: true })
    await run(row)
    expect(left).not.toHaveBeenCalled()
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.delete_failed'))
  })

  it('keeps the row when the server does not carry the field', async () => {
    /* A page rebuilt against a still-running older engine: saying nothing is not
       saying "nothing was there", so the pre-field behaviour stands. */
    const { run, left, toast } = harness({ deleted: null })
    await run(row)
    expect(left).not.toHaveBeenCalled()
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.delete_failed'))
  })

  it('reports a transport failure without dropping the row', async () => {
    const { run, left, toast } = harness(new Error('not connected'))
    await run(row)
    expect(left).not.toHaveBeenCalled()
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.delete_failed'))
  })
})

const bulkSource = (() => {
  const src = readFileSync('src/live/130-writes.js', 'utf8')
  const begin = src.indexOf('DS.sessions.deleteAll = async () => {')
  return src.slice(begin, src.indexOf('\n};', begin) + 3)
})()

/* The bulk path reaches for the rail's own helpers; each is a spy so the test
   can read which rows it decided were gone. */
function bulkHarness(answers: Record<string, Answer | Error>) {
  const rows = Object.keys(answers).map((id) => ({ id, title: id }))
  let live = rows.slice()
  const DS = { sessions: {} as { deleteAll(): Promise<void> } }
  const rpc = {
    call: vi.fn(async (_m: string, p: { session_id: string }) => {
      const a = answers[p.session_id]
      if (a instanceof Error) throw a
      return a
    }),
  }
  const build = new Function(
    'DS', 'rpc', 'T', 'toast', 'sessionRows', 'sessionReplace', 'sessionSet',
    'startDraft', 'drawSettings', 'dropDraft',
    `${bulkSource}\nreturn DS.sessions.deleteAll;`,
  ) as (...args: unknown[]) => () => Promise<void>
  const deleteAll = build(
    DS, rpc,
    (key: string, vars?: Record<string, unknown>) => (vars ? `${key}|${JSON.stringify(vars)}` : key),
    vi.fn(),
    () => live,
    (next: { id: string; title: string }[]) => { live = next },
    vi.fn(), vi.fn(), vi.fn(), vi.fn(),
  )
  return { deleteAll, left: () => live.map((s) => s.id) }
}

describe('clearing every session from the rail', () => {
  it('keeps the rows whose files survived their removal', async () => {
    /* The reported break: a refusal is a resolved promise, so awaiting it
       without reading the answer counted it as a removal. The row vanished and
       came back on the next reload. */
    const { deleteAll, left } = bulkHarness({
      gone: { deleted: 'gone', still_on_disk: false },
      kept: { deleted: null, still_on_disk: true },
    })
    await deleteAll()
    expect(left()).toEqual(['kept'])
  })

  it('drops a row that had no file to remove', async () => {
    /* Same split the single delete makes: nothing left to remove is the
       reader's own goal, so the row goes. */
    const { deleteAll, left } = bulkHarness({ lazy: { deleted: null, still_on_disk: false } })
    await deleteAll()
    expect(left()).toEqual([])
  })

  it('keeps a row when the server does not carry the field', async () => {
    const { deleteAll, left } = bulkHarness({ old: { deleted: null } })
    await deleteAll()
    expect(left()).toEqual(['old'])
  })

  it('keeps a row the transport never answered for', async () => {
    const { deleteAll, left } = bulkHarness({ down: new Error('not connected') })
    await deleteAll()
    expect(left()).toEqual(['down'])
  })
})
