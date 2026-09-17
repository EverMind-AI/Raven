// @vitest-environment happy-dom
/* What the pipeline does with a frame that is not a turn event: the two sheets
 * the engine raises, the question it asks mid-turn, and the stream envelope
 * for a conversation the reader is not looking at.
 *
 * An approval that closed because nobody answered says so. The frame always
 * carried `reason`; the handler dropped it, so a sheet that expired vanished
 * exactly like one the reader had answered. What the reader then saw was the
 * run reporting a system error about an approval that had merely lapsed.
 *
 * Driven through the pipeline's own handlers rather than a copy of them: a copy
 * would go on passing after the shipped one changed.
 */

import { describe, expect, it } from 'vitest'

import { fakeGateway, loadPart } from '../../../scripts/module-harness.mjs'

import type { Sources } from '../sources'

type Pipeline = typeof import('./pipeline')

interface Row { id: string; status?: string | null }
interface Sheet { kind: string; owner: string | null; answer: (...args: unknown[]) => void }

async function harness({ rows = [] as Row[], current = 'tui:open' as string | null } = {}) {
  const seen = {
    toasts: [] as string[],
    closed: [] as string[],
    turns: [] as Array<[string, string]>,
    refreshes: 0,
    touched: [] as string[],
    events: [] as unknown[],
    sheets: [] as Sheet[],
    sent: [] as Array<[string, unknown]>,
  }
  /* The modules under test are imported deepest first, which is the order that
     keeps one module graph: the fakes are installed around the modules the
     first import reaches. */
  await loadPart(async () => {
    await import('./runtime'); await import('./stages')
    return import('./pipeline')
  }, {
    fakes: {
      'src/features/composer/mount': {
        drawMeter: () => {},
        goPaint: () => {},
      },
      'src/features/rail/store': { draw: () => {} },
      'src/state/session/rows': {
        sess: (id: string) => rows.find((r) => r.id === id),
        replace: () => {},
        rows: () => rows,
      },
      'src/features/composer/clarify': {
        close: (id: string) => seen.closed.push(id),
        open: (_p: unknown, answer: (...a: unknown[]) => void) =>
          seen.sheets.push({ kind: 'clarify', owner: null, answer }),
      },
      'src/features/composer/approve': {
        closeApproval: (id: string) => seen.closed.push(id),
        openApproval: (_opts: unknown, answer: (...a: unknown[]) => void, owner: string | null) =>
          seen.sheets.push({ kind: 'approval', owner, answer }),
        open: (_prompt: string, yes: () => void, no: () => void, owner: string | null) =>
          seen.sheets.push({ kind: 'confirm', owner, answer: (ok?: unknown) => (ok ? yes() : no()) }),
      },
      'src/i18n/t': { T: (key: string) => key },
      'src/shell/session': { current: () => current },
      'src/shell/toast': { show: (text: string) => seen.toasts.push(text) },
      /* The phase event goes to the conversation it names, whether or not that
         conversation is on screen -- which is the residency rule. */
      'src/state/session/residency': {
        dispatchTo: (owner: string, event: { type: string }) => seen.turns.push([owner, event.type]),
      },
      'src/features/rail/source': { touchSession: (id: string) => seen.touched.push(id) },
      'src/state/session/stages': { dispatch: (ev: unknown) => seen.events.push(ev) },
    },
    islands: {
      rail: { reconcile: (_cur: Row[], next: Row[]) => ({ rows: next, currentMissing: false }) },
    },
  })
  const pipeline = (await import('./pipeline')) as Pipeline
  const registry = await import('./registry')
  /* A re-read of the rail is a `session.list` and nothing else, so the count is
     read off the transport rather than off a stand-in -- and kept out of the
     traffic the cases below assert on. */
  const transport = await fakeGateway((method: string, params: unknown) => {
    if (method === 'session.list') { seen.refreshes += 1; return Promise.resolve({ sessions: [] }) }
    seen.sent.push([method, params])
    return Promise.resolve({})
  })
  const { setSources } = await import('../sources')
  setSources({ composer: {}, sessions: {}, transcript: {} } as unknown as Partial<Sources>)
  pipeline.installPipeline()
  return {
    pipeline,
    transport,
    seen,
    rows,
    /* The open step is the conversation on screen's, which is the whole of the
       defect the last case pins. */
    live: registry.viewRuntime() as unknown as { st: { hasQA?: boolean } | null; subId: string | null },
    /* The frames a conversation holds while it is off screen, addressed the way
       the parked-turn map used to be. */
    parkedTurns: {
      set: (key: string, pk: { events: unknown[] }) => { registry.ensure(key).events = pk.events },
    },
    subSession: new Proxy({} as Record<string, string>, {
      set: (_t, id: string, key: string) => { registry.record(key, id); return true },
    }),
    sheet: (kind: string) => seen.sheets.find((s) => s.kind === kind)!,
    tick: () => new Promise((r) => setTimeout(r, 0)),
  }
}

async function run(reason: string) {
  const h = await harness()
  h.transport.emit('approval.closed', { approval_id: 'a1', conversation_id: 'tui:one', reason })
  return h.seen
}

describe('an approval sheet closing', () => {
  it('says so when the request expired unanswered', async () => {
    const seen = await run('timeout')

    expect(seen.toasts).toEqual(['gui.confirm.lapsed'])
  })

  it('says so when the transport failed, which is the other nobody-answered', async () => {
    expect((await run('error')).toasts).toEqual(['gui.confirm.lapsed'])
  })

  it('stays quiet for a close the reader caused', async () => {
    /* Three of the four reasons are a person: the frame carries the choice
       itself. Telling someone what they just did is noise. */
    for (const reason of ['allow', 'deny', 'deny_stop', 'cancelled']) {
      expect((await run(reason)).toasts, reason).toEqual([])
    }
  })

  it('still closes the sheet and releases the turn, whatever the reason', async () => {
    /* The notice is added beside the old behaviour, not in place of it: a sheet
       left open over the composer is worse than an unexplained one. */
    for (const reason of ['timeout', 'allow']) {
      const seen = await run(reason)
      expect(seen.closed, reason).toEqual(['a1'])
      expect(seen.turns, reason).toEqual([['tui:one', 'resume']])
    }
  })
})

/* N6: the phase change for a conversation the reader is NOT looking at.
   Nothing above repaints for that case, so the row is the reader's only
   possible notice -- and it takes a full session.list to draw. */
describe('a phase change on a background conversation', () => {
  it('marks the row asking and re-reads the whole list', async () => {
    const h = await harness({ rows: [{ id: 'away' }], current: 'tui:open' })

    h.pipeline.notify('away', { type: 'wait' })

    expect(h.seen.turns).toEqual([['away', 'wait']])
    expect(h.rows[0]!.status).toBe('ask')
    expect(h.seen.touched).toEqual(['away'])
    expect(h.seen.refreshes).toBe(1)
  })

  it('puts a row that was asking back to run, not to nothing', async () => {
    /* The turn that raised the question is still open, so the marker goes back
       to running rather than clearing. */
    const h = await harness({ rows: [{ id: 'away', status: 'ask' }], current: 'tui:open' })

    h.pipeline.notify('away', { type: 'resume' })

    expect(h.rows[0]!.status).toBe('run')
    expect(h.seen.refreshes).toBe(1)
  })

  it('paints the page and stops when the conversation IS the open one', async () => {
    const h = await harness({ rows: [{ id: 'tui:open' }], current: 'tui:open' })

    h.pipeline.notify('tui:open', { type: 'wait' })

    expect(h.seen.turns).toEqual([['tui:open', 'wait']])
    expect(h.rows[0]!.status).toBeUndefined()
    expect(h.seen.refreshes).toBe(0)
  })
})

/* N7: the `event` envelope, whose four paths are the whole of how a background
   turn survives a session switch. */
describe('the stream envelope', () => {
  it('hands the frame to the dispatcher when it names the visible subscription', async () => {
    const h = await harness()
    h.live.st = null
    h.subSession['sub:open'] = 'tui:open'

    h.pipeline.stream({ subscription_id: 'sub:open', event: { type: 'token.delta' } })

    expect(h.seen.events).toEqual([{ type: 'token.delta' }])
  })

  it('drops a frame whose conversation kept no turn', async () => {
    /* No parked record means nothing is holding that conversation's turn, so
       there is nowhere for the frame to be replayed from. */
    const h = await harness()
    h.subSession['sub:away'] = 'away'

    h.pipeline.stream({ subscription_id: 'sub:away', event: { type: 'token.delta' } })

    /* The subscription is known -- it is the buffer that is missing. */
    const registry = await import('./registry')
    expect(registry.bySubscription('sub:away')).toBe('away')
    expect(h.seen.events).toEqual([])
  })

  it('stops buffering at four thousand frames', async () => {
    const h = await harness()
    h.subSession['sub:away'] = 'away'
    const pk = { events: Array.from({ length: 4000 }, () => ({ type: 'token.delta' })) }
    h.parkedTurns.set('away', pk)

    h.pipeline.stream({ subscription_id: 'sub:away', event: { type: 'token.delta' } })

    expect(pk.events).toHaveLength(4000)
  })

  it('marks the row done or failed when a background turn ends', async () => {
    /* This branch only ever runs for a conversation the reader is not looking
       at, so a clean finish is news too -- and a cancel is a stop somebody
       chose, not a failure. */
    for (const [event, status] of [
      [{ type: 'message.complete' }, 'done'],
      [{ type: 'error', payload: { reason: 'boom' } }, 'err'],
      [{ type: 'error', payload: { reason: 'cancelled_by_client' } }, 'done'],
    ] as Array<[Record<string, unknown>, string]>) {
      const h = await harness({ rows: [{ id: 'away' }] })
      h.subSession['sub:away'] = 'away'
      h.parkedTurns.set('away', { events: [] })

      h.pipeline.stream({ subscription_id: 'sub:away', event })

      expect(h.rows[0]!.status, status).toBe(status)
      expect(h.seen.touched, status).toEqual(['away'])
      expect(h.seen.refreshes, status).toBe(1)
    }
  })
})

/* N8: which conversation a side-channel request belongs to, and the wait it
   pairs with the answer's resume. */
describe('the conversation a request is filed under', () => {
  it('waits and resumes on the conversation a confirm names', async () => {
    const h = await harness({ current: 'tui:open' })

    h.pipeline.confirmRequest({ request_id: 'c1', prompt: 'ok?', conversation_id: 'tui:asker' })
    expect(h.seen.turns).toEqual([['tui:asker', 'wait']])
    expect(h.sheet('confirm').owner).toBe('tui:asker')

    h.sheet('confirm').answer(true)
    await h.tick()

    expect(h.seen.turns).toEqual([['tui:asker', 'wait'], ['tui:asker', 'resume']])
    expect(h.seen.sent).toEqual([['confirm.respond', { request_id: 'c1', answer: true }]])
  })

  it('waits and resumes on the conversation an approval names', async () => {
    const h = await harness({ current: 'tui:open' })

    h.pipeline.approvalRequest({ approval_id: 'a1', command: 'rm -rf', conversation_id: 'tui:asker' })
    expect(h.seen.turns).toEqual([['tui:asker', 'wait']])
    expect(h.sheet('approval').owner).toBe('tui:asker')

    h.sheet('approval').answer('allow', 'why not', 'rm *')
    await h.tick()

    expect(h.seen.turns).toEqual([['tui:asker', 'wait'], ['tui:asker', 'resume']])
    expect(h.seen.sent).toEqual([['approval.respond', {
      approval_id: 'a1', choice: 'allow', session_id: 'tui:asker', feedback: 'why not', pattern: 'rm *',
    }]])
  })

  it('waits and resumes on the conversation a clarify names', async () => {
    const h = await harness({ current: 'tui:open' })

    h.pipeline.clarifyRequest({ request_id: 'q1', question: 'which one?', conversation_id: 'tui:asker' })
    expect(h.seen.turns).toEqual([['tui:asker', 'wait']])

    h.sheet('clarify').answer('the second')
    await h.tick()

    expect(h.seen.turns).toEqual([['tui:asker', 'wait'], ['tui:asker', 'resume']])
    expect(h.seen.sent).toEqual([['clarify.respond', { request_id: 'q1', answer: 'the second' }]])
  })

  it('docks where the reader is when the frame names no conversation', async () => {
    /* A dispatch with no conversation to name keeps the old fallback. */
    const h = await harness({ current: 'tui:open' })

    h.pipeline.confirmRequest({ request_id: 'c1', prompt: 'ok?' })
    h.pipeline.clarifyClosed({ request_id: 'q1' })

    expect(h.seen.turns).toEqual([['tui:open', 'wait'], ['tui:open', 'resume']])
    expect(h.seen.closed).toEqual(['q1'])
  })
})

/* N9: characterisation of a BUG that stays in this stage -- the answer marks
   the step of whichever conversation is on screen, not of the one that asked.
   The design's issue list carries the fix; the rewrite keeps today's
   behaviour, and this is what says so. */
describe('the step a clarify answer marks', () => {
  it('marks the OPEN conversation step, even when another one asked', async () => {
    const h = await harness({ current: 'tui:open' })
    const openStep = { hasQA: false }
    h.live.st = openStep

    h.pipeline.clarifyRequest({ request_id: 'q1', question: 'which one?', conversation_id: 'tui:asker' })
    h.sheet('clarify').answer('the second')
    await h.tick()

    expect(openStep.hasQA).toBe(true)
  })
})
