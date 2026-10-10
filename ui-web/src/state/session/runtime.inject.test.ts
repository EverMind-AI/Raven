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

import type { Sources } from '../sources'

type Runtime = typeof import('./runtime')

interface Row { id: string; title?: string }

async function harness({ reject = null as { code?: number; message?: string } | null, draft = false } = {}) {
  const log: unknown[][] = []
  const queue: string[] = []
  const asked: Array<[string, unknown]> = []
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
        noteRow: (labelText: string, detail: string, opts?: Record<string, unknown>) =>
          log.push(['noteRow', labelText, detail, opts ? Object.keys(opts).sort() : null]),
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
        queuePush: (text: string) => { queue.push(text) },
        queueRestore: (next: string[]) => { queue.length = 0; queue.push(...next) },
        queueShift: () => queue.shift(),
        queueSnapshot: () => [...queue],
        turn,
        claimDraft: () => {},
      },
      'src/lib/duration': { formatDuration: (ms: number) => `${ms}ms` },
      'src/i18n/t': { t: (key: string) => key },
      'src/lib/dom': { $: looseQuery() },
      'src/lib/session': { current: () => 's1', setCurrent: () => {} },
      'src/features/rail/title': { plainTitle: (s: unknown) => String(s) },
      'src/state/toast': { show: () => {} },
      'src/state/ctxChip': { set: () => {} },
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
  await fakeGateway((method: string, params: unknown) => {
    asked.push([method, params])
    if (method === 'turn.send' && reject) return Promise.reject(Object.assign(new Error(reject.message || 'no'), reject))
    /* The promotion hangs here, which is the window under test: the draft is
       becoming a conversation and there is no session key yet. */
    if (method === 'session.create') return new Promise<Record<string, unknown>>(() => {})
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
    queue,
    asked,
    sends: () => asked.filter(([m]) => m === 'turn.send').map(([, p]) => p as Record<string, unknown>),
    did: (name: string) => log.filter((c) => c[0] === name),
    tick: () => new Promise((r) => setTimeout(r, 0)),
  }
}

afterEach(() => { turn._resetForTests() })

describe('the files a message hands to the turn', () => {
  it('answers an upload path as the file the upload wrote', async () => {
    /* The note keeps `uploads/shot.png` -- the bubble renders it and `/file`
       re-roots it -- while the turn is handed the absolute file, because
       `viewer_root` reads the session's own root before agent home: a
       same-named file appearing there after the upload would otherwise answer
       for it (lib/uploadPaths). */
    const h = await harness()
    /* The registry the loaded graph reads: `loadPart` resets the module
       registry, so the case's own import has to be the one the graph took. */
    const uploads = await import('../../lib/uploadPaths')
    uploads.remember('uploads/shot.png', '/home/me/.raven/workspace/uploads/shot.png')

    h.runtime.send('look\n\n[attachments, saved in the workspace]\n- uploads/shot.png')
    await h.tick()

    expect(h.sends()[0]?.media).toEqual(['/home/me/.raven/workspace/uploads/shot.png'])
  })

  it('leaves a path no upload minted to the resolver', async () => {
    const h = await harness()

    h.runtime.send('look\n\n[attachments, saved in the workspace]\n- uploads/dragged.png')
    await h.tick()

    expect(h.sends()[0]?.media).toEqual(['uploads/dragged.png'])
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
       a mid-turn message has always landed, with the retry row beside it. */
    const h = await harness({ reject: { code: -32602, message: 'unknown field: busy' } })
    turn.dispatch({ type: 'stream', cancellable: true })

    h.runtime.send('only the last quarter')
    await h.tick()

    expect(h.queue).toEqual(['only the last quarter'])
    expect(h.did('noteRow')).toEqual([['noteRow', 'gui.err.send', 'unknown field: busy', ['retry']]])
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

    expect(h.queue).toEqual(['only the last quarter'])
    expect(h.did('noteRow')).toHaveLength(1)
  })

  it('falls back to the queue when the socket is down', async () => {
    const h = await harness({ reject: { message: 'not connected' } })
    turn.dispatch({ type: 'stream', cancellable: true })

    h.runtime.send('only the last quarter')
    await h.tick()

    expect(h.queue).toEqual(['only the last quarter'])
    expect(h.did('noteRow')).toEqual([['noteRow', 'gui.err.send', 'gui.err.disconnected', ['retry']]])
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
    expect(h.queue).toEqual(['only the last quarter'])
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
    expect(h.queue).toEqual(['only the last quarter'])
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
