// @vitest-environment happy-dom
/* The rail's own data: how a listed session becomes a row, what the stamp on
 * it reads, and what a re-read of the list is allowed to do.
 *
 * All of it is real logic with no test behind it -- a clock with four branches,
 * a title with four fallbacks, a scheduler wrapper the row has to see past --
 * so this is characterisation ahead of the rewrite that moves the shaping into
 * this feature's own source module.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { fakeGateway, loadPart, looseQuery } from '../../../scripts/module-harness.mjs'

import type { Sources } from '../../state/sources'


interface Row { id: string; title?: string; at?: number; last?: string; when?: string }

const label = (key: string, vars?: unknown) => (vars ? `${key}:${JSON.stringify(vars)}` : key)

/* The shaping and the row clock moved to this feature's own source module, and
   the tool-preview pair to the transcript's, which is where wire knowledge
   about a result belongs. `part` is the two together, so the names the cases
   read are unchanged. */
async function harness({ rows = [] as Row[], cur = null as string | null } = {}) {
  const log: unknown[][] = []
  /* The registry is imported first and the seams after it, which is the order
     that keeps one module graph: a mock consulted from inside another mock's
     factory would hand a cycle-mate the unmocked module. */
  await loadPart(() => import('../../state/session/registry'), {
    fakes: {
      'src/features/rail/store': {
        draw: () => log.push(['sessionDraw']),
      },
      'src/state/session/rows': {
        sess: (id: string) => rows.find((r) => r.id === id),
        replace: (next: Row[]) => log.push(['sessionReplace', next]),
        rows: () => rows,
      },
      'src/i18n/t': {
        t: label,
      },
      'src/lib/dom': {
        $: looseQuery(),
      },
    },
  })
  const rail = await import('./source')
  const transcript = await import('../transcript/source')
  const part = {
    whenLabel: rail.whenLabel,
    rowFrom: rail.rowFrom,
    rowPreview: rail.rowPreview,
    loadCronNames: rail.loadCronNames,
    touchSession: rail.touchSession,
    cleanPreview: transcript.cleanPreview,
    okOf: transcript.okOf,
  }
  return { part, log, rows, cur }
}

afterEach(() => vi.useRealTimers())

/* N13: the stamp on a row. A clock only earns its place on today's rows. */
describe('when a row last changed', () => {
  it('reads a clock today, a word for the two days before, and a date past that', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(2026, 5, 15, 12, 0, 0))
    const { part } = await harness()
    const at = (...a: [number, number, number, number, number]) => new Date(...a).getTime() / 1000

    expect(part.whenLabel(at(2026, 5, 15, 9, 5))).toBe('09:05')
    expect(part.whenLabel(at(2026, 5, 14, 23, 0))).toBe('gui.time.yest')
    expect(part.whenLabel(at(2026, 5, 13, 10, 0))).toBe('gui.time.dbyest')
    expect(part.whenLabel(at(2026, 5, 1, 10, 0))).toBe('gui.time.md:{"y":2026,"m":6,"d":1}')
    /* The year only appears once it is not this one. */
    expect(part.whenLabel(at(2025, 11, 25, 10, 0))).toBe('gui.time.ymd:{"y":2025,"m":12,"d":25}')
  })
})

describe('a listed session as a row', () => {
  it('takes the title, the preview and the pin as they come', async () => {
    const { part } = await harness()

    const row = part.rowFrom({
      id: 'tui:20260610_100000_a1',
      updated_at: 1_780_000_000,
      started_at: 1_770_000_000,
      title: 'Cut a release',
      last_message_preview: 'the last thing said',
      message_count: 4,
      pinned: true,
      workdir: '/w/thesis',
    })

    /* Conversation activity, not creation: updated_at wins. */
    expect(row).toMatchObject({
      id: 'tui:20260610_100000_a1',
      title: 'Cut a release',
      last: 'the last thing said',
      at: 1_780_000_000,
      run: null,
      live: true,
      pin: true,
      persisted: true,
      from: undefined,
      workdir: '/w/thesis',
    })
  })

  it('falls back through the preview, the id and the message count', async () => {
    const { part } = await harness()

    const titled = part.rowFrom({ id: 'tui:a', started_at: 1, preview: 'a preview long enough to be cut at twenty-four' })
    expect(titled.title).toBe('a preview long enough to')
    expect(titled.last).toBe('a preview long enough to be cut at twenty-four')

    const bare = part.rowFrom({ id: 'tui:20260610_100000_a1', message_count: 7 })
    expect(bare.title).toBe('gui.sess.fallback_title:{"id":"20260610_100000"}')
    expect(bare.last).toBe('gui.sess.n_messages:{"n":7}')
    expect(bare.workdir).toBeNull()
  })

  it('sees past the scheduler wrapper, and borrows the job name when it has one', async () => {
    const { part } = await harness()
    const wrapped = { id: 'cron:j1', source: 'cron', started_at: 1, preview: "[Scheduled Task] Timer fired. Task 'Morning brief' is due" }

    /* No job list read yet: the task's own words start after "Task '". */
    expect(part.rowFrom(wrapped)).toMatchObject({ title: 'Morning brief', from: 'cron' })
    /* A wrapper with no inner quotes keeps the text with the prefix stripped. */
    expect(part.rowFrom({ ...wrapped, preview: '[Scheduled Task] Timer fired' }).title).toBe('Timer fired')

    await fakeGateway(async () => ({ jobs: [{ id: 'j1', name: 'The morning brief' }] }))
    await part.loadCronNames()

    expect(part.rowFrom(wrapped).title).toBe('The morning brief')
  })

  it('previews a send as its first line, cut at sixty', async () => {
    const { part } = await harness()

    expect(part.rowPreview('  first line  \nsecond line')).toBe('first line')
    expect(part.rowPreview('x'.repeat(80))).toHaveLength(60)
    expect(part.rowPreview(null)).toBe('')
  })
})

describe('touching a row', () => {
  it('moves it to the top under what it just said, and redraws', async () => {
    const rows: Row[] = [
      { id: 'a', at: 1, last: 'old' },
      { id: 'b', at: 2, last: 'newer' },
    ]
    const { part, log } = await harness({ rows })

    part.touchSession('a', 'just asked this\nand more')

    expect(rows.map((r) => r.id)).toEqual(['a', 'b'])
    expect(rows[0]!.last).toBe('just asked this')
    expect(rows[0]!.at).toBeGreaterThan(2)
    expect(log).toContainEqual(['sessionDraw'])
  })

  it('leaves the line alone when there is nothing to preview', async () => {
    const rows: Row[] = [{ id: 'a', at: 1, last: 'kept' }]
    const { part } = await harness({ rows })

    part.touchSession('a')

    expect(rows[0]!.last).toBe('kept')
    expect(rows[0]!.at).toBeGreaterThan(1)
  })
})

describe('a tool result on its way to a preview', () => {
  it('drops the prompt-injection guards and keeps the rest', async () => {
    const { part } = await harness()

    expect(part.cleanPreview('[BEGIN UNTRUSTED CONTENT]\nthe answer\n[END UNTRUSTED CONTENT]'))
      .toBe('the answer')
    expect(part.cleanPreview(null)).toBe('')
  })

  it('reads an error-shaped result as a failure, and anything else as a success', async () => {
    const { part } = await harness()

    expect(part.okOf('exec', 'all good')).toBe(true)
    expect(part.okOf('exec', 'Error: no such file')).toBe(false)
    expect(part.okOf('exec', '  traceback (most recent call last)')).toBe(false)
    expect(part.okOf('exec', 'nothing to see [Analyze the error above')).toBe(false)
    /* Per-file failures the media tool reports inline. */
    expect(part.okOf('understand_media', 'read two [could not understand: c.png')).toBe(false)
    expect(part.okOf('exec', 'errors are mentioned later on')).toBe(true)
  })
})

/* N14: re-reading the list, which is what a background turn's news arrives
   through. */
async function refreshHarness({
  rows = [] as Row[],
  cur = 'a' as string | null,
  answer = { sessions: [] } as unknown,
  reconciled = null as { rows: Row[]; currentMissing: boolean } | null,
}) {
  const log: unknown[][] = []
  const replaced: Row[][] = []
  await loadPart(() => import('../../state/session/registry'), {
    fakes: {
      'src/features/rail/store': {
        draw: () => log.push(['sessionDraw']),
        reconcileRows: (current: Row[], next: Row[], at: string | null) => {
          log.push(['reconcile', current.map((r) => r.id), next.map((r) => r.id), at])
          return reconciled || { rows: next, currentMissing: false }
        },
      },
      'src/features/composer/mount': {
        turn: { dispatch: () => {} },
      },
      'src/state/session/rows': {
        sess: (id: string) => rows.find((r) => r.id === id),
        replace: (next: Row[]) => { replaced.push(next); log.push(['sessionReplace', next.map((r) => r.id)]) },
        rows: () => rows,
      },
      'src/i18n/t': {
        t: label,
      },
      'src/lib/dom': {
        $: looseQuery(),
      },
      'src/lib/session': { current: () => cur },
      'src/features/rail/wire': {
        leaveDeletedSession: (id: string) => { log.push(['leaveDeleted', id]); return Promise.resolve() },
      },
      'src/features/transcript/mount': {
        nudge: () => {},
        stopStream: () => {},
      },
    },
  })
  const registry = await import('../../state/session/registry')
  const rail = await import('./source')
  await fakeGateway(async () => answer)
  const { setSources } = await import('../../state/sources')
  setSources({ composer: {}, rail: {}, transcript: {} } as unknown as Partial<Sources>)
  return { registry, rail, log, replaced }
}

describe('re-reading the session list', () => {
  it('lets the island reconcile it, then draws what came back', async () => {
    /* Pins and persisted fields come from the server; the running marker is
       client state, which is what the reconcile is for. */
    const h = await refreshHarness({
      rows: [{ id: 'a' }],
      answer: { sessions: [{ id: 'a', started_at: 1 }, { id: 'b', started_at: 2 }] },
    })

    await h.registry.refreshList()

    expect(h.log).toContainEqual(['reconcile', ['a'], ['a', 'b'], 'a'])
    expect(h.log).toContainEqual(['sessionReplace', ['a', 'b']])
    expect(h.log).toContainEqual(['sessionDraw'])
    expect(h.log.filter((c) => c[0] === 'leaveDeleted')).toEqual([])
  })

  it('leaves the open conversation when the list no longer has it', async () => {
    const h = await refreshHarness({
      rows: [{ id: 'a' }],
      answer: { sessions: [{ id: 'b', started_at: 2 }] },
      reconciled: { rows: [{ id: 'b' }], currentMissing: true },
    })

    await h.registry.refreshList()

    expect(h.log).toContainEqual(['leaveDeleted', 'a'])
    /* The leave draws for itself; this path does not. */
    expect(h.log.filter((c) => c[0] === 'sessionDraw')).toEqual([])
  })

  it('a plain re-read draws the rows it just replaced, in that order', async () => {
    /* Replacing the rows is not showing them. The archive page's restore
       reaches the rail only through this function, so without the draw the
       restored conversation came back on the server and on disk and stayed off
       the screen until the page was reloaded. Asserted as a sequence: a draw
       before the replace paints the list the reader already had. */
    const h = await refreshHarness({
      rows: [{ id: 'a' }],
      answer: { sessions: [{ id: 'a', started_at: 1 }, { id: 'b', started_at: 2 }] },
    })

    await h.rail.loadSessions()

    expect(h.log.filter((c) => c[0] !== 'reconcile'))
      .toEqual([['sessionReplace', ['b', 'a']], ['sessionDraw']])
  })

  it('a plain re-read hands the list to the reconcile and replaces with what it gets', async () => {
    /* The answer does not carry which conversation has a turn running, nor the
       current one while it is too new to be listed; the reconcile is what
       carries both across. Replacing with the raw list dropped them, and the
       draw above is what makes that visible -- a running badge wiped by
       restoring something from the archive page. */
    const h = await refreshHarness({
      rows: [{ id: 'a' }],
      cur: 'a',
      answer: { sessions: [{ id: 'b', started_at: 2 }] },
      reconciled: { rows: [{ id: 'a' }, { id: 'b' }], currentMissing: false },
    })

    await h.rail.loadSessions()

    expect(h.log).toContainEqual(['reconcile', ['a'], ['b'], 'a'])
    expect(h.replaced[0]!.map((r) => r.id)).toEqual(['a', 'b'])
  })

  it('keeps the stale list when the read fails', async () => {
    const h = await refreshHarness({ rows: [{ id: 'a' }] })
    const { setGateway } = await import('../../rpc/gateway')
    const transport = await import('../../rpc/fixtureTransport')
    const broken = new transport.FixtureTransport({})
    broken.call = () => Promise.reject(new Error('not connected'))
    setGateway(broken)

    await h.registry.refreshList()

    expect(h.log).toEqual([])
  })
})
