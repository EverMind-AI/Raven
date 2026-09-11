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
