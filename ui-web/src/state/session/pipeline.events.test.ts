// @vitest-environment happy-dom
/* One case per stage of the pipeline.
 *
 * Nothing drove this before the stages were written: the arms of the if/else
 * chain they replaced were read off the source, which says what an arm contains
 * and never what it does. So this is characterisation -- what each stage does,
 * ordering included -- and it is the baseline the rewrite was measured against.
 */

import { describe, expect, it } from 'vitest'

import { fakeGateway, loadPart, looseQuery } from '../../../scripts/module-harness.mjs'

import type { Sources } from '../sources'

type Pipeline = typeof import('./pipeline')

interface Row { id: string; title?: string; status?: string | null; last?: string; naming?: boolean }
interface Step {
  seal(): void
  thinkAppend(text: string): void
  sayDelta(text: string): void
  tool(name: string, args: unknown, display: unknown, callId: string): { done(...a: unknown[]): void }
}

async function harness({
  busy = false,
  phase = 'idle',
  rows = [] as Row[],
  current = 's1' as string | null,
} = {}) {
  const log: unknown[][] = []
  const steps: Step[] = []
  const bubbles: Array<{ id: number; text: string; midTurn: boolean }> = []
  let bubbleId = 0
  document.body.innerHTML = '<div id="stage"></div><div id="cronPage"></div>'
  const step = (): Step => {
    const st: Step = {
      seal: () => log.push(['seal']),
      thinkAppend: (text: string) => log.push(['thinkAppend', text]),
      sayDelta: (text: string) => log.push(['sayDelta', text]),
      tool: (name: string, args: unknown, display: unknown, callId: string) => {
        log.push(['tool', name, args, display, callId])
        return { done: (...a: unknown[]) => log.push(['done', ...a]) }
      },
    }
    steps.push(st)
    return st
  }
  /* The stage table is imported first and the pipeline that reaches it after,
     which is the order that keeps one module graph: the fakes are installed
     around the module under test, and a module the first import did not reach
     is loaded afterwards without them. */
  await loadPart(async () => { await import('./runtime'); await import('./stages'); return import('./pipeline') }, {
    fakes: {
      'src/state/ws': { setOpen: () => {} },
      'src/state/session/conversation': {
        /* Answers the id the bubble was drawn under, and keeps the bubbles the
           stage is holding: a mid-turn one may be taken back. */
        ask: (text: string, _when?: string, opts?: { midTurn?: boolean } | null) => {
          log.push(['ask', text])
          bubbleId += 1
          bubbles.push({ id: bubbleId, text, midTurn: !!(opts && opts.midTurn) })
          return bubbleId
        },
        unask: (id: number) => {
          log.push(['unask', id])
          const at = bubbles.findIndex((b) => b.id === id)
          if (at >= 0) bubbles.splice(at, 1)
        },
        noteRow: (labelText: string, detail: string, opts?: Record<string, unknown>) =>
          log.push(['noteRow', labelText, detail, opts ? Object.keys(opts).sort() : null]),
      },
      'src/features/rail/store': {
        draw: () => log.push(['sessionDraw']),
        reconcileRows: (_cur: Row[], next: Row[]) => ({ rows: next, currentMissing: false }),
      },
      'src/state/sheetRack': { session: () => 'sheet-key' },
      'src/state/session/rows': {
        sess: (id: string) => rows.find((r) => r.id === id),
        replace: () => {},
        rows: () => rows,
      },
      'src/features/composer/mount': {
        drawMeter: () => log.push(['drawMeter']),
        goPaint: () => log.push(['goState']),
        queueShift: () => { log.push(['queueShift']); return undefined },
        turn: {
          busy: () => busy,
          phase: () => phase,
          dispatch: (event: { type: string; cancellable?: boolean }) =>
            log.push(['dispatch', event.type, event.cancellable]),
        },
      },
      'src/lib/duration': { formatDuration: (ms: number) => `${ms}ms` },
      'src/i18n/t': {
        t: (key: string, vars?: unknown) => (vars ? `${key}:${JSON.stringify(vars)}` : key),
      },
      'src/lib/dom': { $: looseQuery() },
      'src/lib/session': { current: () => current },
      'src/state/toast': { show: (text: string) => log.push(['toast', text]) },
      'src/state/ctxChip': { set: (used: unknown, max: unknown) => log.push(['setCtx', used, max]) },
      'src/lib/notifications': { show: (title: string) => log.push(['notify', title]) },
      'src/features/rail/title': { plainTitle: (s: unknown) => String(s) },
      'src/features/workspace/record': {
        wsOnTool: (name: string, args: unknown, replay: boolean) => log.push(['wsOnTool', name, args, replay]),
        wsOnToolDone: (...a: unknown[]) => log.push(['wsOnToolDone', ...a]),
      },
      'src/features/rail/source': {
        touchSession: (id: string, preview?: string) => log.push(['touch', id, preview]),
      },
      'src/features/transcript/mount': {
        nudge: () => {},
        stopStream: () => {},
        delivered: (row: Record<string, unknown>) => log.push(['delivered', row]),
        delivery: (turnNo: unknown, metadata: unknown, callId: unknown) =>
          log.push(['delivery', turnNo, metadata, callId]),
        spawnFeed: (p: unknown) => log.push(['spawnFeed', p]),
        finishTurn: (_st: unknown, _steps: unknown, clock: unknown) => log.push(['finishTurn', clock]),
        artifacts: (turnNo: unknown) => log.push(['artifacts', turnNo]),
        /* The fold a stop makes asks this first and nothing else does, so it is
           where "softStop ran" is visible from outside. */
        turnKept: () => { log.push(['softStop']); return false },
        dagFeed: (type: string) => log.push(['dagFeed', type]),
        killStatus: () => log.push(['killStatus']),
        step: step,
        status: (text: string) => log.push(['showStatus', text]),
        failedTurnLabel: () => 'FAILED_LABEL',
      },
      'src/features/transcript/tail': {
        down: () => log.push(['down']),
      },
      'src/features/workspace/store': {
        advanceTurn: () => log.push(['advanceTurn']),
        currentTurn: () => 3,
      },
      'src/features/subagents/store': {
        directEvent: (target: unknown, type: string) => log.push(['directEvent', target, type]),
      },
      'src/features/dag/nodes': {
        fromStarted: () => [{ id: 'first' }, { id: 'last' }],
      },
      'src/features/dag/mount': {
        start: (key: string, run: Record<string, unknown>) => log.push(['dagStart', key, run.run_id]),
        advance: (key: string, p: unknown) => log.push(['dagAdvance', key, p]),
        settle: (key: string, p: unknown) => log.push(['dagSettle', key, p]),
        run: () => null,
      },
    },
  })
  const pipeline = (await import('./pipeline')) as Pipeline
  await fakeGateway(() => Promise.resolve({ sessions: [] }))
  const { setSources } = await import('../sources')
  setSources({ composer: {}, rail: {}, transcript: {} } as unknown as Partial<Sources>)
  const registry = await import('./registry')
  const runtime = await import('./runtime')
  /* The two page-level objects the turn used to live on, as the conversation
     the page is showing: the turn's owner is the runtime a frame lands in, and
     the retry text is that runtime's own. */
  const park = {
    get turnOwner() { return registry.viewRuntime().key },
    get lastAsk() { return registry.viewRuntime().lastAsk },
    set lastAsk(text: string) { registry.viewRuntime().lastAsk = text },
  }
  return {
    dispatch: pipeline.dispatch,
    log,
    steps,
    bubbles,
    rows,
    park,
    live: runtime.state() as unknown as {
      st: Step | null
      steps: Step[]
      say: string
      open: Map<string, unknown>
      sawEpisode: boolean
      startedAt: number
      answerAt: number
    },
    did: (name: string) => log.filter((c) => c[0] === name),
    order: () => log.map((c) => c[0]),
    tick: () => new Promise((r) => setTimeout(r, 0)),
  }
}

/* The guard above every arm (050-turn.js:88). */
describe('a frame addressed to a sub-agent instance', () => {
  it('is handed to the instance and goes no further', async () => {
    const h = await harness()

    h.dispatch({ type: 'token.delta', payload: { target: { agent: 'raven', handle: 'w1' }, text: 'hi' } })

    expect(h.did('directEvent')).toHaveLength(1)
    expect(h.order()).toEqual(['directEvent'])
    expect(h.live.say).toBe('')
  })
})

describe('message.start', () => {
  it('draws the question only in a window that is not already busy (050-turn.js:89)', async () => {
    const h = await harness({ rows: [{ id: 's1' }] })

    h.dispatch({ type: 'message.start', payload: { content: 'do the thing' } })

    expect(h.did('ask')).toEqual([['ask', 'do the thing']])
    expect(h.did('touch')).toEqual([['touch', 's1', 'do the thing']])
    expect(h.park.turnOwner).toBe('s1')
    expect(h.did('dispatch')).toEqual([['dispatch', 'stream', true]])
    expect(h.did('advanceTurn')).toHaveLength(1)
    /* Read BEFORE the phase is set: a window that only watches has not drawn
       the question, and the one that sent it has. */
    const watching = await harness({ busy: true, rows: [{ id: 's1' }] })
    watching.dispatch({ type: 'message.start', payload: { content: 'do the thing' } })
    expect(watching.did('ask')).toEqual([])
    expect(watching.did('dispatch')).toEqual([['dispatch', 'stream', true]])
  })
})

describe('message.injected', () => {
  it('draws the message inside the turn that is running', async () => {
    const h = await harness({ busy: true, rows: [{ id: 's1' }] })
    /* A step is open: the model was narrating when the message arrived. */
    h.dispatch({ type: 'episode.start', payload: {} })
    const open = h.live.st

    h.dispatch({ type: 'message.injected', payload: { turn_id: 't-inject', content: 'only the last quarter' } })

    expect(h.did('ask')).toEqual([['ask', 'only the last quarter']])
    /* Marked as part of the turn under way, which is what keeps the fold, the
       merge of silent steps and the stop note reading it as one. */
    expect(h.bubbles).toEqual([{ id: 1, text: 'only the last quarter', midTurn: true }])
    expect(h.did('touch')).toEqual([['touch', 's1', 'only the last quarter']])
    /* The turn is not re-opened: no workspace turn, no phase change. */
    expect(h.did('advanceTurn')).toEqual([])
    expect(h.did('dispatch')).toEqual([])
    /* The open step is let go of rather than sealed, so what the model says
       next opens a step BELOW the bubble instead of writing into the one that
       was open above it. */
    expect(h.live.st).toBe(null)
    expect(h.did('seal')).toEqual([])
    h.dispatch({ type: 'token.delta', payload: { text: 'right, Q4' } })
    expect(h.live.st).not.toBe(open)
  })

  it('re-files the bubble when the same message opens a turn of its own', async () => {
    /* The host turn ended before its next drain, so the message fell back to a
       turn of its own -- under the id it was announced with. The bubble drawn
       inside the ended turn is taken back and drawn again where this turn
       begins, which is below that turn's answer: one bubble, in the place a
       reload puts it. */
    const h = await harness({ rows: [{ id: 's1' }] })
    h.dispatch({ type: 'message.injected', payload: { turn_id: 't-inject', content: 'only the last quarter' } })
    const [drawn] = h.bubbles

    h.dispatch({ type: 'message.start', payload: { turn_id: 't-inject', content: 'only the last quarter' } })

    expect(h.did('unask')).toEqual([['unask', drawn!.id]])
    expect(h.bubbles).toEqual([{ id: 2, text: 'only the last quarter', midTurn: false }])
    expect(h.did('advanceTurn')).toHaveLength(1)
    expect(h.did('dispatch')).toEqual([['dispatch', 'stream', true]])
  })

  it('re-files it once, however many windows announced it', async () => {
    /* The id is forgotten with the bubble: a second message.start under it --
       a reconnect replaying the buffer -- must not take the new bubble back
       and leave the turn with no question at all. */
    const h = await harness({ rows: [{ id: 's1' }] })
    h.dispatch({ type: 'message.injected', payload: { turn_id: 't-inject', content: 'only the last quarter' } })
    h.dispatch({ type: 'message.start', payload: { turn_id: 't-inject', content: 'only the last quarter' } })

    h.dispatch({ type: 'message.start', payload: { turn_id: 't-inject', content: 'only the last quarter' } })

    expect(h.did('unask')).toHaveLength(1)
    expect(h.bubbles.map((b) => b.text)).toEqual(['only the last quarter', 'only the last quarter'])
  })

  it('still draws a question that was never injected', async () => {
    const h = await harness({ rows: [{ id: 's1' }] })
    h.dispatch({ type: 'message.injected', payload: { turn_id: 't-inject', content: 'only the last quarter' } })

    h.dispatch({ type: 'message.start', payload: { turn_id: 't-other', content: 'a new question' } })

    expect(h.did('ask')).toEqual([['ask', 'only the last quarter'], ['ask', 'a new question']])
  })
})

describe('turn.started', () => {
  it('opens a turn nobody typed, draws its delivery row and re-anchors the clock (050-turn.js:97)', async () => {
    const h = await harness({ rows: [{ id: 's1' }] })
    h.live.startedAt = 0
    h.live.answerAt = 99

    h.dispatch({
      type: 'turn.started',
      payload: { delegated: { kind: 'dag', label: 'qc', status: 'ok', content: 'done', run_id: 'r1' } },
    })

    expect(h.did('advanceTurn')).toHaveLength(1)
    const delivered = h.did('delivered') as Array<[string, Record<string, unknown>]>
    const row = delivered[0]![1]
    expect(row).toMatchObject({ label: 'qc', isDag: true, status: 'ok', body: 'done' })
    expect(typeof row.open).toBe('function')
    /* The stamps are re-anchored here, because no send ran to move them. */
    expect(h.live.startedAt).toBeGreaterThan(0)
    expect(h.live.answerAt).toBe(0)
    expect(h.park.turnOwner).toBe('s1')
    /* A runtime turn is NOT cancellable. */
    expect(h.did('dispatch')).toEqual([['dispatch', 'stream', false]])
  })
})

describe('episode.start', () => {
  it('seals the open step before opening the next one (050-turn.js:131)', async () => {
    const h = await harness()
    h.dispatch({ type: 'token.delta', payload: { text: 'half an answer' } })
    expect(h.live.say).toBe('half an answer')

    h.dispatch({ type: 'episode.start', payload: {} })

    expect(h.order().slice(-1)).not.toContain('sayDelta')
    expect(h.did('seal')).toHaveLength(1)
    expect(h.steps).toHaveLength(2)
    expect(h.live.steps).toHaveLength(2)
    expect(h.live.sawEpisode).toBe(true)
    /* Only the local buffer resets; the step keeps its narration. */
    expect(h.live.say).toBe('')
  })
})

describe('session.titled', () => {
  it('writes the name the server chose, without comparing (050-turn.js:135)', async () => {
    const h = await harness({ rows: [{ id: 's1', title: 'gui.new_task' }] })

    h.dispatch({ type: 'session.titled', payload: { session_id: 's1', title: 'Cut a release' } })

    expect(h.rows[0]!.title).toBe('Cut a release')
    expect(h.rows[0]!.naming).toBe(false)
  })
})

describe('session.naming_ended', () => {
  it('hands the reason to the one place that decides (050-turn.js:139)', async () => {
    const h = await harness({ rows: [{ id: 's1', title: 'gui.new_task' }] })
    const naming = await import('./naming')
    naming.beginNaming('please cut a desktop release')

    h.dispatch({ type: 'session.naming_ended', payload: { session_id: 's1', reason: 'no_title' } })

    expect(h.rows[0]!.title).toBe('please cut a desktop release')
    expect(h.rows[0]!.naming).toBe(false)
  })
})

describe('notice', () => {
  it('ends the turn and leaves the prose where it was said (050-turn.js:146)', async () => {
    const h = await harness()
    h.dispatch({ type: 'thinking.delta', payload: { text: 'thinking' } })

    h.dispatch({ type: 'notice', payload: { kind: 'budget', detail: 'out of tokens' } })

    expect(h.did('killStatus')).toHaveLength(2)
    expect(h.did('seal')).toHaveLength(1)
    expect(h.live.st).toBeNull()
    expect(h.did('noteRow')).toEqual([['noteRow', 'gui.notice.budget', 'out of tokens', ['quiet']]])
  })

  it('names a retry wait on the status line and leaves the turn open', async () => {
    const h = await harness()
    h.dispatch({ type: 'thinking.delta', payload: { text: 'thinking' } })

    h.dispatch({ type: 'notice', payload: { kind: 'llm_retry', detail: 'server', transient: true } })

    expect(h.did('showStatus')).toEqual([['showStatus', 'gui.notice.llm_retry']])
    expect(h.did('noteRow')).toEqual([])
    expect(h.did('seal')).toEqual([])
    expect(h.live.st).not.toBeNull()
  })

  it('lets the next frame of real output take the status line back', async () => {
    const h = await harness()

    h.dispatch({ type: 'notice', payload: { kind: 'llm_retry', detail: 'server', transient: true } })
    h.dispatch({ type: 'token.delta', payload: { text: 'the answer' } })

    expect(h.did('killStatus')).toHaveLength(1)
  })
})

describe('permission.review', () => {
  it('draws nothing: the smart-mode reviewer is not the page\'s business', async () => {
    const h = await harness()

    h.dispatch({ type: 'permission.review', payload: { phase: 'started' } })
    h.dispatch({ type: 'permission.review', payload: { phase: 'ended' } })

    expect(h.did('showStatus')).toEqual([])
    expect(h.did('killStatus')).toEqual([])
  })
})

describe('thinking.delta', () => {
  it('opens a step if there is none and appends the thought (050-turn.js:157)', async () => {
    const h = await harness()

    h.dispatch({ type: 'thinking.delta', payload: { text: 'let me look' } })

    expect(h.steps).toHaveLength(1)
    expect(h.did('thinkAppend')).toEqual([['thinkAppend', 'let me look']])
    expect(h.did('killStatus')).toHaveLength(1)
  })
})

describe('token.delta', () => {
  it('folds the thought, lands the prose and stamps the answer clock (050-turn.js:160)', async () => {
    const h = await harness()
    h.live.answerAt = 0

    h.dispatch({ type: 'token.delta', payload: { text: 'the ' } })
    h.dispatch({ type: 'token.delta', payload: { text: 'answer' } })

    expect(h.did('sayDelta')).toEqual([['sayDelta', 'the '], ['sayDelta', 'answer']])
    expect(h.live.say).toBe('the answer')
    expect(h.live.answerAt).toBeGreaterThan(0)
  })
})

describe('tool.start', () => {
  it('binds the row to the call id and hands the panel the whole argument object (050-turn.js:167)', async () => {
    const h = await harness()
    const args = { path: 'a.py', old_text: 'x', new_text: 'y' }

    h.dispatch({
      type: 'tool.start',
      payload: { name: 'edit_file', arguments: args, display: 'a.py', tool_call_id: 'c1' },
    })

    expect(h.did('tool')).toEqual([['tool', 'edit_file', args, 'a.py', 'c1']])
    expect(h.live.open.get('c1')).toMatchObject({ name: 'edit_file', args })
    /* The whole object, not the one-line display string. */
    expect(h.did('wsOnTool')).toEqual([['wsOnTool', 'edit_file', args, false]])
  })
})

describe('tool.complete', () => {
  it('records a delivery before the early return, and closes the row it finds (050-turn.js:178)', async () => {
    const h = await harness()
    h.dispatch({
      type: 'tool.start',
      payload: { name: 'exec', arguments: { command: 'ls' }, tool_call_id: 'c1' },
    })

    /* An id this page never opened: the metadata is still recorded, and then
       the arm returns. */
    h.dispatch({ type: 'tool.complete', payload: { tool_call_id: 'nope', metadata: { files: 1 } } })
    expect(h.did('delivery')).toEqual([['delivery', 3, { files: 1 }, 'nope']])
    expect(h.did('done')).toEqual([])

    h.dispatch({
      type: 'tool.complete',
      payload: {
        tool_call_id: 'c1',
        ok: false,
        result_preview: '[BEGIN UNTRUSTED CONTENT]\nall good\n[END UNTRUSTED CONTENT]',
        truncated: true,
        diff: '@@ -1 +1 @@',
        file_change: { path: '/w/a.md', after: 'now' },
        file_removed: [{ path: '/w/old.md', before: 'was here' }],
        file_written: [{ path: '/w/made.txt', created: true, size: 9, lines: 1 }],
      },
    })

    /* The emit site's verdict is authoritative, the guard markers are stripped,
       and the row is gone from the open map. */
    const [, ok, preview, took, nothing, truncated] =
      (h.did('done') as Array<[string, boolean, string, number, null, boolean]>)[0]!
    expect(ok).toBe(false)
    expect(preview).toBe('all good')
    expect(typeof took).toBe('number')
    expect(nothing).toBeNull()
    expect(truncated).toBe(true)
    expect(h.live.open.has('c1')).toBe(false)
    expect(h.did('wsOnToolDone')[0]!.slice(1, 4)).toEqual(['exec', { command: 'ls' }, false])
    /* Everything the tool reported about files: the diff, the payload that says
       whether there was a file under the write at all, the files this call made
       vanish, and the ones a listing found it had left behind -- which for an
       `exec` are the only report there is. */
    expect(h.did('wsOnToolDone')[0]!.slice(6))
      .toEqual(['@@ -1 +1 @@', { path: '/w/a.md', after: 'now' },
        [{ path: '/w/old.md', before: 'was here' }],
        [{ path: '/w/made.txt', created: true, size: 9, lines: 1 }]])
  })
})

describe('message.complete', () => {
  it('folds the turn (050-turn.js:192)', async () => {
    const h = await harness({ rows: [{ id: 's1', title: 'a deck' }] })
    h.dispatch({ type: 'token.delta', payload: { text: 'the answer' } })

    h.dispatch({ type: 'message.complete', payload: { usage: { context_used: 10, context_max: 100 }, duration_ms: 2000 } })

    expect(h.did('finishTurn')).toEqual([['finishTurn', '2000ms']])
    /* The products close the turn, after the answer. */
    expect(h.order().indexOf('artifacts')).toBeGreaterThan(h.order().indexOf('finishTurn'))
    expect(h.did('dispatch')).toEqual([['dispatch', 'idle', undefined]])
    expect(h.did('setCtx')).toEqual([['setCtx', 10, 100]])
    expect(h.rows[0]!.last).toBe('the answer')
    expect(h.did('notify')).toEqual([['notify', 'gui.set.ntf.done']])
    expect(h.live.say).toBe('')
  })
})

describe('error', () => {
  it('idles the turn and offers the retry the last send recorded (050-turn.js:198)', async () => {
    const h = await harness()
    h.park.lastAsk = 'send this again'

    h.dispatch({ type: 'error', payload: { message: 'the model refused', detail: 'try later' } })

    expect(h.did('dispatch')).toEqual([['dispatch', 'idle', undefined]])
    expect(h.did('noteRow')).toEqual([['noteRow', 'the model refused', 'try later', ['retry']]])
    expect(h.did('softStop')).toEqual([])
  })

  it('ends a died turn the way a stop does: the fold, then the note in the replay\'s words, then the products', async () => {
    const h = await harness()
    h.park.lastAsk = 'send this again'
    h.dispatch({ type: 'episode.start', payload: { index: 0 } })

    h.dispatch({
      type: 'error',
      payload: { message: 'turn_failed', reason: 'internal', detail: 'Error calling LLM (first_byte_timeout): no first byte' },
    })

    expect(h.did('finishTurn')).toHaveLength(1)
    expect(h.did('noteRow')).toEqual([
      ['noteRow', 'FAILED_LABEL', 'Error calling LLM (first_byte_timeout): no first byte', ['retry']],
    ])
    const order = h.order()
    expect(order.indexOf('finishTurn')).toBeLessThan(order.indexOf('noteRow'))
    expect(order.indexOf('artifacts')).toBeGreaterThan(order.indexOf('noteRow'))
    expect(h.did('dispatch')).toEqual([['dispatch', 'idle', undefined]])
    expect(h.live.st).toBeNull()
    expect(h.live.steps).toEqual([])
    expect(h.park.lastAsk).toBe('send this again')
    expect(h.did('queueShift')).toHaveLength(1)
  })
})

describe('cron.delivered', () => {
  it('says a scheduled run produced something (050-turn.js:212)', async () => {
    const h = await harness()

    h.dispatch({ type: 'cron.delivered', payload: { name: 'morning brief' } })

    expect(h.did('toast')).toEqual([['toast', 'gui.cron.new_output:{"name":"morning brief"}']])
  })
})

describe('cron.missed', () => {
  it('reports the payload count once, not once per job (050-turn.js:214)', async () => {
    const h = await harness()

    h.dispatch({ type: 'cron.missed', payload: { count: 3 } })

    expect(h.did('toast')).toEqual([['toast', 'gui.cron.missed_x:{"count":3}']])
  })
})

describe('subagent.status', () => {
  it('feeds the spawn card through the island (050-turn.js:219)', async () => {
    const h = await harness()
    const payload = { instance: 'w1', agent: 'raven', label: 'qc', status: 'running' }

    h.dispatch({ type: 'subagent.status', payload })

    expect(h.did('spawnFeed')).toEqual([['spawnFeed', payload]])
  })
})

describe('subagent.delivered', () => {
  it('does nothing: the row belongs to the turn it opens (050-turn.js:227)', async () => {
    const h = await harness()

    h.dispatch({ type: 'subagent.delivered', payload: { label: 'qc' } })

    expect(h.log).toEqual([])
  })
})

describe('dag.run_started', () => {
  it('feeds the trail card before the sheet is started (050-turn.js:254)', async () => {
    const h = await harness()

    h.dispatch({ type: 'dag.run_started', payload: { run_id: 'r1', nodes: [] } })

    expect(h.order()).toEqual(['dagFeed', 'dagStart'])
    expect(h.did('dagStart')).toEqual([['dagStart', 'sheet-key', 'r1']])
  })
})

describe('dag.node_updated', () => {
  it('feeds the card, then moves the node through the island (050-turn.js:275)', async () => {
    const h = await harness()
    const payload = { run_id: 'r1', node_id: 'first', status: 'done' }

    h.dispatch({ type: 'dag.node_updated', payload })

    expect(h.order()).toEqual(['dagFeed', 'dagAdvance'])
    expect(h.did('dagAdvance')).toEqual([['dagAdvance', 'sheet-key', payload]])
  })
})

describe('dag.run_completed', () => {
  it('feeds the card, then settles the run (050-turn.js:281)', async () => {
    const h = await harness()
    const payload = { run_id: 'r1', summary: 'all done' }

    h.dispatch({ type: 'dag.run_completed', payload })

    expect(h.order()).toEqual(['dagFeed', 'dagSettle'])
    expect(h.did('dagSettle')).toEqual([['dagSettle', 'sheet-key', payload]])
  })
})

describe('dag.run_replanned', () => {
  it('feeds the card alone: the sheet waits for the new run (050-turn.js:284)', async () => {
    const h = await harness()

    h.dispatch({ type: 'dag.run_replanned', payload: { run_id: 'r1' } })

    expect(h.order()).toEqual(['dagFeed'])
  })
})

/* The tasks store no longer sits behind an import here (import-direction's
   PINNED list) -- these five frames reach it, if at all, through the seam
   `app/install.ts` grows `sources.tasks` with. */
describe('the tasks seam behind the five task-shaped events', () => {
  const statusPayload = { instance: 'w1', agent: 'raven', label: 'qc', status: 'running' }
  const startedPayload = { run_id: 'r1', nodes: [] }
  const updatedPayload = { run_id: 'r1', node_id: 'first', status: 'done' }
  const completedPayload = { run_id: 'r1', summary: 'all done' }
  const replannedPayload = { run_id: 'r1' }

  it('fans each arm out to an installed tasks source, with the payload it received', async () => {
    const h = await harness()
    const { setSources } = await import('../sources')
    const seen: Record<string, unknown> = {}
    setSources({
      tasks: {
        onSubagentStatus: (p: unknown) => { seen.onSubagentStatus = p },
        onRunStarted: (p: unknown) => { seen.onRunStarted = p },
        onNodeUpdated: (p: unknown) => { seen.onNodeUpdated = p },
        onRunCompleted: (p: unknown) => { seen.onRunCompleted = p },
        onRunReplanned: (p: unknown) => { seen.onRunReplanned = p },
      },
    } as unknown as Partial<Sources>)

    h.dispatch({ type: 'subagent.status', payload: statusPayload })
    h.dispatch({ type: 'dag.run_started', payload: startedPayload })
    h.dispatch({ type: 'dag.node_updated', payload: updatedPayload })
    h.dispatch({ type: 'dag.run_completed', payload: completedPayload })
    h.dispatch({ type: 'dag.run_replanned', payload: replannedPayload })

    expect(seen).toEqual({
      onSubagentStatus: statusPayload,
      onRunStarted: startedPayload,
      onNodeUpdated: updatedPayload,
      onRunCompleted: completedPayload,
      onRunReplanned: replannedPayload,
    })
  })

  /* The regression the `sources.tasks?.` guard exists for: these suites drive
     the hot turn path without installing a tasks source (`harness()` above
     never sets one), and `ds('tasks')` would throw where the optional read
     does not. */
  it('drops all five silently with no tasks source installed', async () => {
    const h = await harness()

    expect(() => {
      h.dispatch({ type: 'subagent.status', payload: statusPayload })
      h.dispatch({ type: 'dag.run_started', payload: startedPayload })
      h.dispatch({ type: 'dag.node_updated', payload: updatedPayload })
      h.dispatch({ type: 'dag.run_completed', payload: completedPayload })
      h.dispatch({ type: 'dag.run_replanned', payload: replannedPayload })
    }).not.toThrow()
  })
})

/* The three contract members with no arm at all. */
describe('a frame no arm names', () => {
  it('is dropped without a sound', async () => {
    const h = await harness()

    for (const type of ['tool.progress', 'dag.node_stalled', 'media']) {
      h.dispatch({ type, payload: {} })
    }

    expect(h.log).toEqual([])
  })
})
