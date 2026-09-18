// @vitest-environment happy-dom
/* What a reader can do TO a conversation: clear it, compact it, fork it,
 * delete it, archive it, pin it, rename it.
 *
 * Clear and compact are fire-and-answer over a conversation the reader can
 * leave, and both used to ask for the session pointer again in the reply --
 * which by then can name a different conversation than the one the action was
 * for. Driven through the real handlers, because the defect is which
 * conversation the reply lands on and nothing about the text of either
 * function says that.
 */

import { describe, expect, it, vi } from 'vitest'

import { fakeGateway, loadPart, looseQuery } from '../../../scripts/module-harness.mjs'

import type { Sources } from '../sources'

type Runtime = typeof import('./runtime')
type Wiring = typeof import('../../app/install')

interface Row { id: string; title?: string; last?: string; pin?: boolean }

/* ---- /clear and /compress, and the fork the answer block offers ---------- */

async function harness({ rows }: { rows: Row[] }) {
  const calls: unknown[][] = []
  document.body.innerHTML = '<div id="stage"><p>a conversation</p></div>'
  const stage = document.getElementById('stage') as HTMLElement
  let current: string | null = 'a'
  let settle: { res: (v: unknown) => void; rej: (e: unknown) => void } | null = null
  const slash = [{ id: 'gui.clear' }, { id: 'gui.compress' }]
  /* The runtime is imported first and the module that wires it after, which is
     the order that keeps one module graph: the fakes are installed around the
     modules the first import reaches, and one it did not is loaded afterwards
     without them. */
  await loadPart(async () => {
    await import('./runtime')
    return import('../../app/install')
  }, {
    fakes: {
      'src/features/composer/mount': { drawMeter: () => calls.push(['drawMeter']) },
      'src/state/session/conversation': {
        noteRow: (label: string) => {
          calls.push(['noteRow', label])
          return { set: () => {}, remove: () => calls.push(['lineRemove']) }
        },
        noteSay: (_line: unknown, text: string) => calls.push(['noteSay', text]),
        pitch: () => calls.push(['pitch']),
      },
      'src/features/rail/store': {
        draw: () => calls.push(['sessionDraw']),
      },
      'src/state/session/rows': {
        sess: (id: string) => rows.find((r) => r.id === id),
        open: (s: Row) => calls.push(['sessionOpen', s.id]),
        rows: () => rows,
      },
      'src/state/confirm': {
        /* The dialog is not what is under test: say yes at once. */
        ask: (_t: string, _b: string, _l: string, fn: () => void) => fn(),
      },
      'src/i18n/t': {
        /* Enough of the real thing to see WHICH conversation a message names. */
        t: (key: string, vars?: unknown) => (vars ? `${key}:${JSON.stringify(vars)}` : key),
      },
      'src/lib/dom': {
        $: looseQuery(),
      },
      'src/lib/session': {
        current: () => current,
        setCurrent: (id: string | null) => { current = id; calls.push(['sessionSet', id]) },
      },
      'src/features/rail/title': { plainTitle: (t: unknown) => String(t) },
      'src/state/toast': { show: (text: string) => calls.push(['toast', text]) },
      'src/app/updates': { showUpNote: () => {} },
      'src/features/transcript/tail': {
        down: () => calls.push(['down']),
      },
    },
  })
  const runtime = (await import('./runtime')) as Runtime
  await fakeGateway((method: string, params?: { session_id?: string }) => {
    calls.push(['rpc', method, params && params.session_id])
    return new Promise((res, rej) => { settle = { res, rej } })
  })
  const { setSources } = await import('../sources')
  setSources({ composer: { slash }, rail: {}, transcript: {} } as unknown as Partial<Sources>)
  const wiring = await import('../../app/install')
  wiring.installSources()
  wiring.installActions()
  const tick = () => new Promise((r) => setTimeout(r, 0))
  return {
    clear: () => runtime.clear(),
    compress: () => runtime.compress(),
    branch: () => runtime.branch(),
    leaveFor: (id: string) => { current = id },
    ok: (payload?: unknown) => { settle!.res(payload || {}); return tick() },
    fail: (e?: unknown) => {
      settle!.rej(e || Object.assign(new Error('nope'), { data: { detail: 'nope' } }))
      return tick()
    },
    calls,
    rows,
    stageHtml: () => stage.innerHTML,
    did: (name: string) => calls.some((c) => c[0] === name),
  }
}

const ROWS = (): Row[] => [{ id: 'a', last: 'A last line' }, { id: 'b', last: 'B last line' }]

describe('the live session actions', () => {
  it('empties the stage of the conversation it cleared', async () => {
    const rows = ROWS()
    const h = await harness({ rows })

    h.clear()
    await h.ok()

    expect(h.stageHtml()).toBe('')
    expect(h.did('pitch')).toBe(true)
    expect(h.did('drawMeter')).toBe(true)
    expect(rows[0]!.last).toBe('gui.sess.cleared')
  })

  it('leaves another conversation\'s stage alone when the reader has moved on', async () => {
    const rows = ROWS()
    const h = await harness({ rows })

    h.clear()
    h.leaveFor('b')
    await h.ok()

    /* B is on screen and was never cleared. */
    expect(h.stageHtml()).toBe('<p>a conversation</p>')
    expect(h.did('pitch')).toBe(false)
    expect(h.did('drawMeter')).toBe(false)
    /* The row that says "cleared" is A's, because A is what was cleared. */
    expect(rows[0]!.last).toBe('gui.sess.cleared')
    expect(rows[1]!.last).toBe('B last line')
    expect(h.did('sessionDraw')).toBe(true)
  })

  it('reports a clear that failed for the conversation being read', async () => {
    const h = await harness({ rows: ROWS() })

    h.clear()
    await h.fail()

    expect(h.calls).toContainEqual(['noteRow', 'gui.clear_title'])
  })

  it('tells the reader a clear failed, under the name of the session it was for', async () => {
    const rows = ROWS()
    rows[0]!.title = 'Alpha'
    const h = await harness({ rows })

    h.clear()
    h.leaveFor('b')
    await h.fail()

    /* Not on B's transcript, which did not refuse anything -- and not nowhere
       either: dropped, the reader walks away believing A was wiped. */
    expect(h.calls.filter((c) => c[0] === 'noteRow')).toEqual([])
    const said = h.calls.filter((c) => c[0] === 'toast')
    expect(said).toHaveLength(1)
    expect(said[0]![1]).toContain('gui.sess.clear_failed')
    expect(said[0]![1]).toContain('Alpha')
    expect(said[0]![1]).toContain('nope')
  })

  it('reports a compaction that failed for the conversation being read', async () => {
    const h = await harness({ rows: ROWS() })

    const done = h.compress()
    await h.fail()
    await done

    expect(h.calls).toContainEqual(['lineRemove'])
    expect(h.calls.some((c) => c[0] === 'noteRow' && String(c[1]).startsWith('gui.compress.fail'))).toBe(true)
    expect(h.did('down')).toBe(true)
  })

  it('tells the reader a compaction failed, under the name of the session it was for', async () => {
    const rows = ROWS()
    rows[0]!.title = 'Alpha'
    const h = await harness({ rows })

    const done = h.compress()
    h.leaveFor('b')
    await h.fail()
    await done

    /* `line` is a segment in the lane this started in, which the switch has
       already dropped -- but a bare noteRow asks for the CURRENT lane, so the
       error used to land over the conversation being read. */
    expect(h.calls.some((c) => c[0] === 'noteRow' && String(c[1]).startsWith('gui.compress.fail'))).toBe(false)
    const said = h.calls.filter((c) => c[0] === 'toast')
    expect(said).toHaveLength(1)
    expect(said[0]![1]).toContain('gui.sess.compress_failed')
    expect(said[0]![1]).toContain('Alpha')
    expect(h.did('down')).toBe(false)
  })

  it('does not scroll another conversation after a compaction the reader left', async () => {
    const h = await harness({ rows: ROWS() })

    const done = h.compress()
    h.leaveFor('b')
    await h.ok({ removed: 3, before_tokens: 100, after_tokens: 40 })
    await done

    /* The success path reaches further than the failure path: the failure guard
       returns before this, so only a compaction that SUCCEEDS after a switch
       can scroll the wrong transcript to its end. */
    expect(h.did('down')).toBe(false)
  })

  it('still writes a finished compaction onto its own conversation', async () => {
    const h = await harness({ rows: ROWS() })

    const done = h.compress()
    await h.ok({ removed: 3, before_tokens: 100, after_tokens: 40 })
    await done

    const said = h.calls.find((c) => c[0] === 'noteSay')
    expect(said![1]).toContain('gui.compress.done')
    expect(h.calls.filter((c) => c[0] === 'toast')).toEqual([])
    expect(h.did('down')).toBe(true)
  })
})

/* N15: the fork the answer block's footer offers. */
describe('forking the open conversation', () => {
  it('puts the fork at the top of the rail and opens it', async () => {
    const rows = ROWS()
    const h = await harness({ rows })

    h.branch()
    await h.ok({ session_id: 'a-fork', title: 'Alpha (fork)', message_count: 7 })

    expect(h.calls).toContainEqual(['rpc', 'session.branch', 'a'])
    expect(rows[0]).toMatchObject({ id: 'a-fork', title: 'Alpha (fork)', last: 'gui.sess.branched', live: true })
    expect(h.calls).toContainEqual(['sessionSet', 'a-fork'])
    expect(h.calls).toContainEqual(['sessionOpen', 'a-fork'])
    expect(h.calls.filter((c) => c[0] === 'toast').map((c) => String(c[1]).split(':')[0]))
      .toEqual(['gui.sess.branched_n'])
  })

  it('says so, and adds no row, when there was nothing to fork', async () => {
    /* A conversation whose first turn never saved answers no session_id. */
    const rows = ROWS()
    const h = await harness({ rows })

    h.branch()
    await h.ok({})

    expect(rows.map((r) => r.id)).toEqual(['a', 'b'])
    expect(h.calls).toContainEqual(['toast', 'gui.sess.branch_empty'])
  })
})

/* ---- delete, archive, pin, rename: the rail's own verbs ------------------ */

interface Answer {
  deleted?: string | null
  still_on_disk?: boolean
  archived?: boolean
  session_key?: string
}

/* The rail verbs as the page's wiring and its session source install them.
   The rest of those parts reaches for dozens of collaborators that have
   nothing to do with the decision under test, so those are fakes; the decision
   is the part's own. The rail store's `removeSessionRow` is what "the row goes"
   means -- nothing else in leaveDeletedSession is visible from outside it. */
async function railHarness(
  answer: Answer | Error | ((method: string, params: Record<string, unknown>) => unknown),
  { rows = [] as Row[], current = null as string | null } = {},
) {
  const left = vi.fn()
  const toast = vi.fn()
  const draws = vi.fn()
  const asked: Array<[string, Record<string, unknown>]> = []
  /* `remove` does not return the confirm callback's promise, so the harness
     holds it: without it the assertions run before the toast is written. */
  let settled: Promise<void> = Promise.resolve()
  await loadPart(async () => {
    await import('./runtime')
    return import('../../app/install')
  }, {
    fakes: {
      'src/state/session/rows': {
        replace: () => {},
        rows: () => rows,
        sess: (id: string) => rows.find((r) => r.id === id),
      },
      'src/features/rail/store': {
        draw: draws,
        endRename: () => {},
        removeSessionRow: (next: Row[], _current: string | null, id: string) => {
          left(id)
          return { kind: 'unchanged', rows: next }
        },
      },
      'src/state/sheetRack': {
        forget: () => {},
      },
      'src/features/composer/mount': {
        dropDraft: () => {},
      },
      'src/state/confirm': {
        ask: (_t: string, _b: string, _l: string, run: () => Promise<void>) => {
          settled = run()
        },
      },
      'src/i18n/t': {
        t: label,
      },
      'src/lib/dom': {
        $: looseQuery(),
      },
      'src/lib/session': { current: () => current, setCurrent: (id: string | null) => { current = id } },
      'src/state/toast': { show: toast },
      'src/features/dag/mount': {
        forget: () => {},
      },
    },
  })
  const runtime = (await import('./runtime')) as Runtime
  await fakeGateway(async (method: string, params: Record<string, unknown> = {}) => {
    asked.push([method, params])
    if (typeof answer === 'function') return answer(method, params)
    if (answer instanceof Error) throw answer
    return answer
  })
  const { setSources } = await import('../sources')
  setSources({ composer: { slash: [] }, rail: {}, transcript: {} } as unknown as Partial<Sources>)
  const wiring = (await import('../../app/install')) as Wiring
  const { sessionsSource } = await import('../../features/rail/source')
  /* The pin is a verb of the session source itself, which the boot installs. */
  setSources({ rail: sessionsSource } as unknown as Partial<Sources>)
  wiring.installActions()
  const tick = () => new Promise((r) => setTimeout(r, 0))
  return {
    remove: async (s: Row) => { runtime.remove(s as never); await settled },
    archive: async (s: Row) => { await runtime.archive(s as never); await tick() },
    pin: async (id: string, pinned: boolean) => { runtime.pin(id, pinned); await tick() },
    rename: async (id: string, title: string, previous: string) => {
      runtime.rename(id, title, previous)
      await tick()
    },
    rows,
    left,
    toast,
    draws,
    asked,
  }
}

const label = (key: string, vars?: Record<string, unknown>) =>
  (vars ? `${key}|${JSON.stringify(vars)}` : key)

const row = { id: 'tui:20260610_100000_a1', title: 'a deck' }

describe('deleting a session from the rail', () => {
  it('drops the row when the file was removed', async () => {
    const { remove, left, toast } = await railHarness({ deleted: row.id, still_on_disk: false })
    await remove(row)
    expect(left).toHaveBeenCalledWith(row.id)
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.deleted_x'))
  })

  it('drops the row when there was nothing to remove', async () => {
    /* The case that could not be cleared at all before: a conversation whose
       first turn never saved a file answers a null `deleted`, and the reader's
       goal -- the row gone -- already holds. */
    const { remove, left, toast } = await railHarness({ deleted: null, still_on_disk: false })
    await remove(row)
    expect(left).toHaveBeenCalledWith(row.id)
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.delete_absent'))
  })

  it('keeps the row when the file survived its removal', async () => {
    const { remove, left, toast } = await railHarness({ deleted: null, still_on_disk: true })
    await remove(row)
    expect(left).not.toHaveBeenCalled()
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.delete_failed'))
  })

  it('keeps the row when the server does not carry the field', async () => {
    /* A page rebuilt against a still-running older engine: saying nothing is not
       saying "nothing was there", so the pre-field behaviour stands. */
    const { remove, left, toast } = await railHarness({ deleted: null })
    await remove(row)
    expect(left).not.toHaveBeenCalled()
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.delete_failed'))
  })

  it('reports a transport failure without dropping the row', async () => {
    const { remove, left, toast } = await railHarness(new Error('not connected'))
    await remove(row)
    expect(left).not.toHaveBeenCalled()
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.delete_failed'))
  })
})

/* N16: archiving, and the undo the toast offers. */
describe('archiving a session from the rail', () => {
  it('takes the row off the rail and offers the undo', async () => {
    const rows = [{ id: 'keep' }, row]
    const { archive, left, toast } = await railHarness({ archived: true, session_key: row.id }, { rows })

    await archive(row)

    expect(left).toHaveBeenCalledWith(row.id)
    const [text, action] = toast.mock.calls[0] as [string, { label: string; fn: () => Promise<void> }]
    expect(text).toContain('gui.sess.archived')
    expect(action.label).toBe('gui.undo')
  })

  it('puts an undone row back where it was, by index', async () => {
    /* The row is gone from the rail by the time the undo runs, and its place in
       the list is the one thing the answer cannot carry -- so the index is read
       before the call and the row is spliced back at it. */
    const rows = [{ id: 'first' }, row, { id: 'third' }]
    const { archive, toast, draws } = await railHarness(
      (_method, params) => ({ archived: params.archived === true, session_key: row.id }),
      { rows },
    )

    await archive(row)
    /* What `leaveArchivedSession` leaves behind: the rail no longer lists it. */
    rows.splice(rows.indexOf(row), 1)
    expect(rows.map((r) => r.id)).toEqual(['first', 'third'])
    draws.mockClear()

    const action = (toast.mock.calls[0] as [string, { fn: () => Promise<void> }])[1]
    await action.fn()

    expect(rows.map((r) => r.id)).toEqual(['first', row.id, 'third'])
    expect(draws).toHaveBeenCalled()
  })
})

/* N17: the two optimistic writes, and what a refusal puts back. */
describe('a refused pin or rename', () => {
  it('turns the pin back over and says so', async () => {
    const pinned = { id: 'p1', title: 'a deck', pin: true }
    const { pin, toast, rows, asked } = await railHarness(new Error('no such method'), { rows: [pinned] })

    await pin('p1', true)

    expect(asked[0]).toEqual(['session.pin', { session_id: 'p1', pinned: true }])
    expect(rows[0]!.pin).toBe(false)
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.pin_failed'))
  })

  it('puts the old name back on the row and in the heading', async () => {
    document.body.innerHTML = '<h1 id="title">new name</h1>'
    const named = { id: 'r1', title: 'new name' }
    const { rename, toast, rows, asked } = await railHarness(
      new Error('too long'),
      { rows: [named], current: 'r1' },
    )

    await rename('r1', 'new name', 'old name')

    expect(asked[0]).toEqual(['session.title', { session_id: 'r1', title: 'new name' }])
    expect(rows[0]!.title).toBe('old name')
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.sess.rename_failed'))
  })
})

/* The bulk path reaches for the rail's own helpers; each is a fake so the test
   can read which rows it decided were gone. */
async function bulkHarness(answers: Record<string, Answer | Error>) {
  let live = Object.keys(answers).map((id) => ({ id, title: id }))
  await loadPart(async () => {
    await import('../../features/rail/wire')
    return import('../../app/boot')
  }, {
    fakes: {
      'src/state/session/rows': {
        replace: (next: Row[]) => { live = next as Array<{ id: string; title: string }> },
        rows: () => live,
        sess: (id: string) => live.find((r) => r.id === id),
      },
      'src/features/composer/mount': {
        dropDraft: () => {},
      },
      'src/i18n/t': {
        t: label,
      },
      'src/lib/dom': {
        $: looseQuery(),
      },
      'src/state/session/registry': { switchToDraft: () => {} },
      'src/lib/session': { current: () => null, setCurrent: () => {} },
      'src/state/toast': { show: () => {} },
      'src/features/settings/store': {
        redraw: () => {},
      },
    },
  })
  const runtime = (await import('./runtime')) as Runtime
  await fakeGateway(async (_method: string, p: { session_id: string }) => {
    const a = answers[p.session_id]
    if (a instanceof Error) throw a
    return a
  })
  const { setSources } = await import('../sources')
  const { sessionsSource } = await import('../../features/rail/source')
  setSources({ rail: sessionsSource } as unknown as Partial<Sources>)
  /* The three writes that also move the reader are installed onto the source
     rather than built into it, the way the page installs them. */
  const { installSessionActions } = await import('../../features/rail/wire')
  installSessionActions()
  return { deleteAll: () => runtime.deleteAll(), left: () => live.map((s) => s.id) }
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
