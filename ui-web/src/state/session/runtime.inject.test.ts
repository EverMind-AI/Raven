// @vitest-environment happy-dom
/* A message typed while the turn is still running.
 *
 * It goes to that turn -- the server merges it at the turn's next gap -- so the
 * send must leave the turn alone: no bubble drawn here (`message.injected`
 * draws it in every window at once), no stage reset, no phase moved. What the
 * page does when the gateway refuses is the other half: the message is still in
 * front of the reader, so it falls back to the queue that used to hold it.
 */

import { afterEach, describe, expect, it } from 'vitest'

import { fakeGateway, loadPart, looseQuery } from '../../../scripts/module-harness.mjs'
import * as turn from '../../features/composer/turn'
import { readMessage } from '../../lib/attachments'

import type { QueuedMessage } from '../../features/composer/types'
import type { Sources } from '../sources'

type Runtime = typeof import('./runtime')

interface Row { id: string; title?: string }

async function harness({
  reject = null as { code?: number; message?: string } | null, draft = false, current = 's1',
  create = false, failSends = 0,
} = {}) {
  const log: unknown[][] = []
  const notes: Array<Record<string, unknown> | undefined> = []
  const queue: QueuedMessage[] = []
  const asked: Array<[string, unknown]> = []
  const delivered: string[] = []
  const rows: Row[] = [{ id: 's1' }]
  document.body.innerHTML = '<h1 id="title"></h1><div id="stage"></div><div id="flash"></div>'
  await loadPart(async () => {
    await import('./runtime'); await import('./stages')
    return import('./pipeline')
  }, {
    fakes: {
      'src/state/page': { show: () => {} },
      'src/state/ws': { setOpen: () => {}, reset: () => {} },
      'src/state/session/conversation': {
        ask: (text: string) => log.push(['ask', text]),
        unask: (id: number) => log.push(['unask', id]),
        noteRow: (labelText: string, detail: string, opts?: Record<string, unknown>) => {
          log.push(['noteRow', labelText, detail, opts ? Object.keys(opts).sort() : null])
          notes.push(opts)
        },
        pitch: () => {},
        /* The real reader, which is what the production stub delegates to: a
           message's note is where the send recovers its files from. */
        splitAtts: (t: string) => readMessage(t),
        unpitch: () => {},
      },
      'src/features/rail/store': { markNew: () => {}, draw: () => {}, endRename: () => {} },
      'src/state/sheetRack': { forget: () => {} },
      'src/state/session/rows': {
        sess: (id: string) => rows.find((r) => r.id === id),
        open: () => {},
        replace: () => {},
        rows: () => rows,
      },
      'src/features/composer/mount': {
        drawMeter: () => {},
        goPaint: () => {},
        loadDraft: () => {},
        parkDraft: () => {},
        queueClear: () => { queue.length = 0 },
        queuePush: (text: string, moveOn?: string) => { queue.push(moveOn ? { text, moveOn } : { text }) },
        queueRestore: (next: QueuedMessage[]) => { queue.length = 0; queue.push(...next) },
        queueShift: () => queue.shift(),
        queueSnapshot: () => [...queue],
        turn,
        claimDraft: () => {},
      },
      'src/lib/duration': { formatDuration: (ms: number) => `${ms}ms` },
      /* The one heading a case reads back filled in: a message about a group
         opens with it, and which group it named is what those cases check. */
      'src/i18n/t': {
        t: (key: string, vars?: Record<string, unknown>) =>
          key === 'gui.tasks.board.answer_head' ? `On "${String(vars?.title)}":` : key,
      },
      'src/lib/dom': { $: looseQuery() },
      'src/lib/session': { current: () => current, setCurrent: () => {} },
      'src/features/rail/title': { plainTitle: (s: unknown) => String(s) },
      'src/state/toast': { show: () => {} },
      'src/state/ctxWindow': { set: () => {} },
      'src/lib/notifications': { show: () => {} },
      'src/state/banner': { draw: () => {} },
      'src/state/tier': { load: () => {} },
      'src/features/workspace/record': { wsOnHistory: () => {} },
      'src/features/rail/source': { rowPreview: (t: string) => t, touchSession: () => {} },
      'src/features/transcript/mount': {
        nudge: () => {},
        /* The whole live stage goes through this one call, so it is where "the
           turn under way was torn down" is visible from outside. */
        stopStream: () => log.push(['stopStream']),
        finishTurn: () => log.push(['foldTurn']),
        artifacts: () => {},
        turnKept: () => true,
        killStatus: () => log.push(['killStatus']),
        step: () => ({ seal: () => {}, sayDelta: () => {}, thinkAppend: () => {} }),
        status: () => {},
      },
      'src/features/transcript/tail': { down: () => {} },
      'src/features/workspace/store': { currentTurn: () => 1, loadDeliveries: () => {} },
      'src/state/session/resume': { resume: () => {}, refreshDag: () => {} },
    },
  })
  const runtime = (await import('./runtime')) as Runtime
  const registry = await import('./registry')
  const pipeline = await import('./pipeline')
  let turnSends = 0
  await fakeGateway((method: string, params: unknown) => {
    asked.push([method, params])
    if (method === 'turn.send') {
      turnSends += 1
      const refusal = reject || (turnSends <= failSends ? { code: -32602, message: 'unknown field: busy' } : null)
      if (refusal) return Promise.reject(Object.assign(new Error(refusal.message || 'no'), refusal))
      delivered.push(String((params as Record<string, unknown>).content))
    }
    /* The promotion hangs here, which is the window under test: the draft is
       becoming a conversation and there is no session key yet. A case that
       needs the promotion to finish asks for `create`. */
    if (method === 'session.create') {
      return create ? { session_id: 's1', info: { cwd: '/w' } } : new Promise<Record<string, unknown>>(() => {})
    }
    return Promise.resolve({})
  })
  const { setSources } = await import('../sources')
  setSources({ composer: { slash: [] }, rail: {}, transcript: {} } as unknown as Partial<Sources>)
  const part = await import('../../app/install')
  part.installActions()
  registry.adopt(draft ? null : 's1')
  return {
    runtime,
    registry,
    log,
    notes,
    queue,
    asked,
    dispatch: pipeline.dispatch,
    sends: () => asked.filter(([m]) => m === 'turn.send').map(([, p]) => p as Record<string, unknown>),
    delivered: () => [...delivered],
    did: (name: string) => log.filter((c) => c[0] === name),
    tick: () => new Promise((r) => setTimeout(r, 0)),
  }
}

afterEach(() => { turn._resetForTests() })

describe('the files a message hands to the turn', () => {
  /* The note is the record of what was handed over, and the turn receives its
     paths as it names them: an upload as the file it wrote, a file attached
     where it already was by the path it was dragged with -- here a session
     root's own file that shares the upload's name. */
  it('hands the turn the paths the note names, as it names them', async () => {
    const h = await harness()

    h.runtime.send('look\n\n[attachments, saved in the workspace]\n'
      + '- /home/me/.raven/workspace/uploads/shot.png\n- uploads/shot.png')
    await h.tick()

    expect(h.sends()[0]?.media).toEqual(['/home/me/.raven/workspace/uploads/shot.png', 'uploads/shot.png'])
  })
})

describe('a message typed while the turn is running', () => {
  it('goes to that turn instead of the queue', async () => {
    const h = await harness()
    turn.dispatch({ type: 'stream', cancellable: true })

    h.runtime.send('only the last quarter')
    await h.tick()

    expect(h.sends()).toEqual([
      { session_key: 's1', content: 'only the last quarter', busy: 'inject' },
    ])
    expect(h.queue).toEqual([])
  })

  it('leaves the turn under way exactly as it was', async () => {
    /* No bubble is drawn from this side: `message.injected` draws it, in every
       window at once, and a second copy here is the one the sender alone would
       see. Nothing is folded or reset either -- the turn is still running. */
    const h = await harness()
    turn.dispatch({ type: 'stream', cancellable: true })

    h.runtime.send('only the last quarter')
    await h.tick()

    expect(h.did('ask')).toEqual([])
    expect(h.did('stopStream')).toEqual([])
    expect(h.did('foldTurn')).toEqual([])
    expect(turn.phase()).toBe('streaming')
  })

  it('falls back to the queue when a gateway does not know the field', async () => {
    /* -32602: an older gateway. The message must not vanish, so it lands where
       a mid-turn message has always landed, with the note that says it waits
       in the queue. */
    const h = await harness({ reject: { code: -32602, message: 'unknown field: busy' } })
    turn.dispatch({ type: 'stream', cancellable: true })

    h.runtime.send('only the last quarter')
    await h.tick()

    expect(h.queue).toEqual([{ text: 'only the last quarter' }])
    expect(h.did('noteRow')).toEqual([['noteRow', 'gui.err.queued', 'unknown field: busy', null]])
    expect(turn.phase()).toBe('streaming')
  })

  it('falls back to the queue when the lane went idle under the send', async () => {
    /* -32003: the turn ended between the busy check and the call, so there was
       nothing to merge into. The queue drains onto the idle lane as a turn of
       its own, which is what the reader wanted either way. */
    const h = await harness({ reject: { code: -32003, message: 'already has an active turn' } })
    turn.dispatch({ type: 'stream', cancellable: true })

    h.runtime.send('only the last quarter')
    await h.tick()

    expect(h.queue).toEqual([{ text: 'only the last quarter' }])
    expect(h.did('noteRow')).toHaveLength(1)
  })

  it('falls back to the queue when the socket is down', async () => {
    const h = await harness({ reject: { message: 'not connected' } })
    turn.dispatch({ type: 'stream', cancellable: true })

    h.runtime.send('only the last quarter')
    await h.tick()

    expect(h.queue).toEqual([{ text: 'only the last quarter' }])
    /* A dropped socket can take the turn with it, so this note promises the
       message, not the end of a turn the page can no longer see. */
    expect(h.did('noteRow')).toEqual([['noteRow', 'gui.err.queued_offline', '', null]])
  })
})

/* The queue is the whole recovery for a message the running turn refused: it
   goes out when the turn ends. A retry beside it sent the same text a second
   time, on top of the copy already waiting -- the two paths did not know about
   each other -- so the note now says where the message went and offers nothing
   to press. */
describe('a mid-turn send the gateway refused', () => {
  it('leaves the text in the queue with no retry to send it again', async () => {
    const h = await harness({ reject: { code: -32602, message: 'unknown field: busy' } })
    turn.dispatch({ type: 'stream', cancellable: true })

    h.runtime.send('only the last quarter')
    await h.tick()

    expect(h.queue).toEqual([{ text: 'only the last quarter' }])
    expect(h.notes).toHaveLength(1)
    expect(h.notes[0]?.retry).toBeUndefined()
    expect(h.did('noteRow')).toEqual([['noteRow', 'gui.err.queued', 'unknown field: busy', null]])
  })

  it('delivers the text once when the turn ends', async () => {
    const h = await harness({ failSends: 1 })
    turn.dispatch({ type: 'stream', cancellable: true })

    h.runtime.send('only the last quarter')
    await h.tick()

    /* The queue is the only copy: there is no second send to make. */
    expect(h.notes[0]?.retry).toBeUndefined()

    turn.dispatch({ type: 'idle' })
    h.runtime.drain()
    await h.tick()

    expect(h.delivered()).toEqual(['only the last quarter'])
  })
})

/* The refusal can land after the turn it was aimed at has folded: the send saw
   a turn, the completion was handled first, and the catch queues into an idle
   engine. The queue holds the only copy, so the engine has to pick it up rather
   than wait for a next turn that may never come. */
describe('a refusal that lands after its turn ended', () => {
  it('sends the queued text once, not twice', async () => {
    const h = await harness({ failSends: 1 })
    turn.dispatch({ type: 'stream', cancellable: true })

    /* No await: the rejection is still in flight when the turn folds. */
    h.runtime.send('only the last quarter')
    h.runtime.finishTurn({})
    await h.tick()

    /* The race, as it stands: the queue holds the message, the engine is idle,
       and nothing has sent it. */
    expect(h.queue).toEqual([{ text: 'only the last quarter' }])
    expect(turn.phase()).toBe('idle')
    expect(h.delivered()).toEqual([])

    /* The fallback drain lets the engine pick it up once the server has
       unwound the turn that ended. */
    await new Promise((r) => setTimeout(r, 450))
    expect(h.delivered()).toEqual(['only the last quarter'])
    expect(h.queue).toEqual([])

    /* A later turn end has nothing left to release. */
    h.runtime.finishTurn({})
    await h.tick()
    expect(h.delivered()).toEqual(['only the last quarter'])
  })
})


describe('a message typed while a brand-new conversation is being made', () => {
  it('waits in the queue instead of going to a turn that does not exist yet', async () => {
    /* There is no turn to merge into until the conversation exists, and no
       session key to name either: sending anyway answered -32602 and drew a red
       failure row over a message the queue then delivered a second later. */
    const h = await harness({ draft: true })

    h.runtime.send('summarise the report')
    await h.tick()
    h.runtime.send('only the last quarter')
    await h.tick()

    expect(h.sends()).toEqual([])
    expect(h.queue).toEqual([{ text: 'only the last quarter' }])
    expect(h.did('noteRow')).toEqual([])
  })

  it('waits while the conversation is still being finished', async () => {
    /* The draft flag is lowered partway through the promotion, with the staged
       writes and the subscription still running -- so "is this a draft" is not
       the whole question, and the send that read only that flag went out
       against a session key of null. */
    const h = await harness({ draft: true })
    h.runtime.send('summarise the report')
    await h.tick()
    h.registry.mint(h.registry.draft(), 's1')

    h.runtime.send('only the last quarter')
    await h.tick()

    expect(h.sends()).toEqual([])
    expect(h.queue).toEqual([{ text: 'only the last quarter' }])
  })
})

/* The composer's recent list (features/model/recent.ts) is written by the send:
   a message the server takes as a turn of its own is a use of the model the chip
   named as it left, and one merged into the running turn rides on the model that
   turn started with, recorded when it did. */
describe('the model a message goes out on', () => {
  const used = (): unknown => JSON.parse(localStorage.getItem('raven.models.used') || 'null')

  it('records nothing when the server refuses the send', async () => {
    localStorage.clear()
    const h = await harness({ reject: { code: -32003, message: 'refused' } })
    const model = await import('../../features/model/store')
    model.setCurrent('z-ai/glm-5.3-flash', 'openrouter')

    h.runtime.send('hello')
    await h.tick()

    expect(h.did('noteRow')).toHaveLength(1)
    expect(used()).toBeNull()
  })

  it('records the model the chip named as the message left, not one picked while it was on its way', async () => {
    localStorage.clear()
    const h = await harness()
    const model = await import('../../features/model/store')
    model.setCurrent('z-ai/glm-5.3-flash', 'openrouter')

    h.runtime.send('hello')
    model.setCurrent('anthropic/claude-sonnet-5', 'openrouter')
    await h.tick()

    expect(used()).toEqual([{ model: 'z-ai/glm-5.3-flash', provider: 'openrouter' }])
  })

  it('heads the recent list when the message starts a turn', async () => {
    localStorage.clear()
    const h = await harness()
    const model = await import('../../features/model/store')
    model.setCurrent('z-ai/glm-5.3-flash', 'openrouter')

    h.runtime.send('hello')
    await h.tick()

    expect(h.sends()).toEqual([{ session_key: 's1', content: 'hello' }])
    expect(used()).toEqual([{ model: 'z-ai/glm-5.3-flash', provider: 'openrouter' }])
  })

  it('leaves the list alone for a message merged into the running turn', async () => {
    localStorage.clear()
    const h = await harness()
    const model = await import('../../features/model/store')
    model.setCurrent('z-ai/glm-5.3-flash', 'openrouter')
    turn.dispatch({ type: 'stream', cancellable: true })

    h.runtime.send('only the last quarter')
    await h.tick()

    expect(h.sends()).toEqual([{ session_key: 's1', content: 'only the last quarter', busy: 'inject' }])
    expect(used()).toBeNull()
  })
})

describe('an imported session', () => {
  it('sends nothing, by any door', async () => {
    const h = await harness({ current: 'imported:' + 'a'.repeat(32) })
    h.runtime.send('can I add to it?')
    await h.tick()
    expect(h.sends()).toEqual([])
  })
})

/* A message sent with a Move on above the composer carries the group with it, so
   the turn it opens is told which matter it moves on and nothing waits between
   the two. It waits for a turn of its own rather than joining one already
   running, which was told about something else. */
describe('a message that moves a task on', () => {
  const naming = (moveOn: string) => (_key: string | null, text: string) => ({ text: `On "Fix":\n${text}`, moveOn })

  it('goes at once, carrying the group it moves on', async () => {
    const h = await harness()
    h.runtime.lendBeforeSend({ take: (key, text) => (key === 's1' ? naming('g_1')(key, text) : null) })

    h.runtime.send('the next step')

    expect(h.sends()).toEqual([{ session_key: 's1', content: 'On "Fix":\nthe next step', move_on: 'g_1' }])
    h.runtime.lendBeforeSend(null)
  })

  /* The reader's bubble is the message as it went, so the heading is on the page
     as it is in the conversation the turn reads. */
  it('is drawn as it goes, with the group named', async () => {
    const h = await harness()
    h.runtime.lendBeforeSend({ take: naming('g_1') })

    h.runtime.send('the next step')

    expect(h.did('ask')).toEqual([['ask', 'On "Fix":\nthe next step']])
    h.runtime.lendBeforeSend(null)
  })

  it('still hands the turn the files its note names', async () => {
    const h = await harness()
    h.runtime.lendBeforeSend({ take: naming('g_1') })

    h.runtime.send('look\n\n[attachments, saved in the workspace]\n- uploads/shot.png')
    await h.tick()

    expect(h.sends()[0]?.media).toEqual(['uploads/shot.png'])
    h.runtime.lendBeforeSend(null)
  })

  it('waits for a turn of its own instead of joining the one running', async () => {
    const h = await harness()
    h.runtime.lendBeforeSend({ take: naming('g_1') })
    turn.dispatch({ type: 'stream', cancellable: true })

    h.runtime.send('the next step')
    await h.tick()

    expect(h.sends()).toEqual([])
    expect(h.queue).toEqual([{ text: 'On "Fix":\nthe next step', moveOn: 'g_1' }])
    h.runtime.lendBeforeSend(null)
  })
})

/* The same, with the Task Board's own bar lent as it is at boot. The bar goes
   with its message (the Task Board spec, 5.1.1), so a message that waits for a
   turn of its own waits with its group: the bar is gone the moment it is sent,
   the next message is free to join the turn that is running, and the queue
   lets each message go with the group it was sent with. */
describe('a message that moves a group on, sent while a turn runs', () => {
  async function busyWithBar() {
    const h = await harness()
    const tasks = await import('../../features/tasks/store')
    h.runtime.lendBeforeSend({ take: tasks.takeMoveOn })
    turn.dispatch({ type: 'stream', cancellable: true })
    return { h, tasks }
  }
  const group = (id: string) => ({ id, title: `Matter ${id}`, summary: '' })

  it('takes the bar into the queue with it', async () => {
    const { h, tasks } = await busyWithBar()
    tasks.moveOn('s1', group('g_a'))

    h.runtime.send('about a')
    await h.tick()

    expect(h.sends()).toEqual([])
    expect(tasks.moveOnFor('s1')).toBeNull()
    h.runtime.lendBeforeSend(null)
  })

  it('leaves the next message free to join the turn that is running', async () => {
    const { h, tasks } = await busyWithBar()
    tasks.moveOn('s1', group('g_a'))
    h.runtime.send('about a')

    h.runtime.send('and one thing more')
    await h.tick()

    expect(h.sends()).toEqual([{ session_key: 's1', content: 'and one thing more', busy: 'inject' }])
    h.runtime.lendBeforeSend(null)
  })

  /* Released both ways the queue is: by the turn finishing, then by a stop. */
  it('leaves the queue with the group it was sent with, not the one held last', async () => {
    const { h, tasks } = await busyWithBar()
    tasks.moveOn('s1', group('g_a'))
    h.runtime.send('about a')
    tasks.moveOn('s1', group('g_b'))
    h.runtime.send('about b')

    h.runtime.finishTurn({})
    await h.tick()
    turn.dispatch({ type: 'idle' })
    h.runtime.drain()
    await h.tick()

    expect(h.sends()).toEqual([
      { session_key: 's1', content: 'On "Matter g_a":\nabout a', move_on: 'g_a' },
      { session_key: 's1', content: 'On "Matter g_b":\nabout b', move_on: 'g_b' },
    ])
    h.runtime.lendBeforeSend(null)
  })
})

/* The draft's first message mints the conversation it goes to, and it leaves
   through a branch of its own. A group taken as the message left the field has
   to travel through that branch too: the bar is gone by then, so a send that
   does not carry it loses the move-on without a trace. */
describe('a message that moves a group on, sent before the conversation exists', () => {
  it('carries the group through the branch that mints the conversation', async () => {
    const h = await harness({ draft: true, create: true })
    h.runtime.lendBeforeSend({ take: (_key, text) => ({ text: `On "Fix":\n${text}`, moveOn: 'g_1' }) })

    h.runtime.send('the next step')
    await h.tick()

    expect(h.sends()).toEqual([{ session_key: 's1', content: 'On "Fix":\nthe next step', move_on: 'g_1' }])
    h.runtime.lendBeforeSend(null)
  })
})

const groupOf = (id: string) => ({ id, title: `Matter ${id}`, summary: '' })

/* A retry re-sends the message that failed, with the group it first went out
   with. It must never take the bar again: by the time the reader presses
   Retry, the Move on above the composer may belong to another message, and
   sending this one with that group is the very mix-up the queue fix removes. */
describe('a retry after a send the gateway refused', () => {
  it('keeps the group the message was sent with, not one picked after the failure', async () => {
    const h = await harness({ reject: { code: -32003, message: 'refused' } })
    const tasks = await import('../../features/tasks/store')
    h.runtime.lendBeforeSend({ take: tasks.takeMoveOn })
    tasks.moveOn('s1', groupOf('g_a'))

    h.runtime.send('about a')
    await h.tick()
    const retry = h.notes[0]!.retry as () => void

    tasks.moveOn('s1', groupOf('g_b'))
    retry()
    await h.tick()

    expect(h.sends()).toEqual([
      { session_key: 's1', content: 'On "Matter g_a":\nabout a', move_on: 'g_a' },
      { session_key: 's1', content: 'On "Matter g_a":\nabout a', move_on: 'g_a' },
    ])
    h.runtime.lendBeforeSend(null)
  })

  it('sends no group when the message that failed had none', async () => {
    const h = await harness({ reject: { code: -32003, message: 'refused' } })
    const tasks = await import('../../features/tasks/store')
    h.runtime.lendBeforeSend({ take: tasks.takeMoveOn })

    h.runtime.send('about nothing')
    await h.tick()
    const retry = h.notes[0]!.retry as () => void

    tasks.moveOn('s1', groupOf('g_b'))
    retry()
    await h.tick()

    expect(h.sends()).toEqual([
      { session_key: 's1', content: 'about nothing' },
      { session_key: 's1', content: 'about nothing' },
    ])
    h.runtime.lendBeforeSend(null)
  })
})

/* The same retry, offered by the turn the message opened when that turn dies
   as it runs. Its message keeps its own group here too, rather than whatever
   bar the reader picked since. */
describe('a retry after a turn that failed', () => {
  it('keeps the group that message was sent with, not one picked after the failure', async () => {
    const h = await harness()
    const tasks = await import('../../features/tasks/store')
    h.runtime.lendBeforeSend({ take: tasks.takeMoveOn })
    tasks.moveOn('s1', groupOf('g_a'))

    h.runtime.send('about a')
    await h.tick()

    tasks.moveOn('s1', groupOf('g_b'))
    h.dispatch({ type: 'error', payload: { message: 'turn_failed', reason: 'internal' } })
    const retry = h.notes[0]!.retry as () => void
    retry()
    await h.tick()

    expect(h.sends()).toEqual([
      { session_key: 's1', content: 'On "Matter g_a":\nabout a', move_on: 'g_a' },
      { session_key: 's1', content: 'On "Matter g_a":\nabout a', move_on: 'g_a' },
    ])
    h.runtime.lendBeforeSend(null)
  })

  it('sends no group when the message that opened the turn had none', async () => {
    const h = await harness()
    const tasks = await import('../../features/tasks/store')
    h.runtime.lendBeforeSend({ take: tasks.takeMoveOn })

    h.runtime.send('about nothing')
    await h.tick()

    tasks.moveOn('s1', groupOf('g_b'))
    h.dispatch({ type: 'error', payload: { message: 'turn_failed', reason: 'internal' } })
    const retry = h.notes[0]!.retry as () => void
    retry()
    await h.tick()

    expect(h.sends()).toEqual([
      { session_key: 's1', content: 'about nothing' },
      { session_key: 's1', content: 'about nothing' },
    ])
    h.runtime.lendBeforeSend(null)
  })
})
