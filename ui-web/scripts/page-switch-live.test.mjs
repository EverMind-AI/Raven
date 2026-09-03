/* The assembled live layer's session switch: what a round trip is allowed to
 * paint once the reader has moved on, and when the new-task screen comes down.
 *
 * Driven through the real openLiveSession / startDraft / subscribe rather than
 * through assertions about their source, because the defect this pins is a
 * matter of ordering between two in-flight opens -- nothing about the text of
 * either function says which of them wins.
 */

import { readFileSync } from 'node:fs'

import { Window } from 'happy-dom'
import { describe, expect, it } from 'vitest'

const build = readFileSync(new URL('../build.py', import.meta.url), 'utf8')
const manifest = build.match(/_LIVE_PARTS = \[(.*?)\n\]/s)
if (!manifest) throw new Error('_LIVE_PARTS is absent from build.py')
const parts = [...manifest[1].matchAll(/"([^"]+\.js)"/g)].map((m) => m[1])
const live = parts
  .map((name) => readFileSync(new URL(`../src/live/${name}`, import.meta.url), 'utf8'))
  .join('')

/* From `mark` through the closing brace of the function `tail` names. */
function span(mark, tail) {
  const start = live.indexOf(mark)
  if (start < 0) throw new Error(`${mark} is absent from the assembled live layer`)
  const head = live.indexOf(tail, start)
  if (head < 0) throw new Error(`${tail} is absent from the assembled live layer`)
  const brace = live.indexOf('{', head)
  let depth = 0
  for (let i = brace; i < live.length; i += 1) {
    if (live[i] === '{') depth += 1
    else if (live[i] === '}') {
      depth -= 1
      if (depth === 0) return live.slice(start, i + 1)
    }
  }
  throw new Error(`${tail} has no closing brace in the assembled live layer`)
}

const NAMES = [
  'rpc', 'live', 'subBySession', 'subSession', 'toast', 'sessionCurrent', 'parkTurn',
  'parkDraft', 'loadDraft', 'sessionSet', 'sessionDraw', '$', 'T', 'pitch', 'unpitch',
  'ta', 'stop_', 'turn', 'queueClear', 'resetTurnState', 'wsReset', 'setWs', 'drawMeter',
  'goState', 'drawBanner', 'sess', 'markNewCurrent', 'plainTitle', 'parkedTurns',
  'restoreTurn', 'wsSetRoot', 'setCtx', 'renderHistory', 'wsOnHistory', 'RavenIslands',
  'loadProviders',
]

function harness({ rows, deferSubscribe } = {}) {
  const document = new Window().document
  const calls = []
  const boxes = {}
  for (const id of ['#title', '#stage', '#flash']) {
    const el = document.createElement('div')
    el.id = id.slice(1)
    boxes[id] = el
  }
  let current = null
  const state = { fresh: '1' }
  const pending = []
  const subs = []
  const env = {
    rpc: {
      call: (method, params) => {
        calls.push(['rpc', method, params && (params.session_id || params.session_key)])
        if (method === 'session.resume') {
          return new Promise((res, rej) => pending.push({ id: params.session_id, res, rej }))
        }
        const answer = { subscription_id: `sub:${params.session_key}` }
        if (!deferSubscribe) return Promise.resolve(answer)
        return new Promise((res) => subs.push({ id: params.session_key, res: () => res(answer) }))
      },
    },
    live: { subId: null },
    subBySession: {},
    subSession: {},
    toast: (text) => calls.push(['toast', text]),
    sessionCurrent: () => current,
    parkTurn: () => calls.push(['parkTurn']),
    parkDraft: () => {},
    loadDraft: (id) => calls.push(['loadDraft', id]),
    sessionSet: (id) => { current = id; calls.push(['sessionSet', id]) },
    sessionDraw: () => calls.push(['sessionDraw']),
    $: (sel) => boxes[sel] || null,
    T: (key) => key,
    pitch: () => { state.fresh = '1'; calls.push(['pitch']) },
    unpitch: () => { state.fresh = null; calls.push(['unpitch']) },
    ta: { focus: () => {} },
    stop_: () => {},
    turn: { dispatch: () => {}, busy: () => false, snapshot: () => ({}), restore: () => {}, reduce: (p) => p },
    queueClear: () => {},
    resetTurnState: () => {},
    wsReset: () => {},
    setWs: () => {},
    drawMeter: () => {},
    goState: () => {},
    drawBanner: () => {},
    sess: (id) => (rows || []).find((r) => r.id === id),
    markNewCurrent: () => {},
    loadProviders: (sid, gen) => calls.push(['loadProviders', sid, gen]),
    plainTitle: (s) => String(s),
    parkedTurns: new Map(),
    restoreTurn: () => calls.push(['restoreTurn']),
    wsSetRoot: (root) => calls.push(['wsSetRoot', root]),
    setCtx: (used) => calls.push(['setCtx', used]),
    renderHistory: (messages) => { state.fresh = null; calls.push(['renderHistory', messages[0]]) },
    wsOnHistory: () => {},
    RavenIslands: {
      workspace: { loadDeliveries: (id) => calls.push(['loadDeliveries', id]) },
      view: { resume: (id) => calls.push(['viewResume', id]) },
    },
  }
  const install = Function(
    'env',
    `const { ${NAMES.join(', ')} } = env;
     ${span('async function subscribe(sessionKey) {', 'function claimStream')}
     ${span('let draft = false;', 'async function openLiveSession')}
     return { subscribe, startDraft, openLiveSession };`,
  )
  const api = install(env)
  return {
    ...api,
    calls,
    env,
    state,
    title: () => boxes['#title'].textContent,
    settle: (id, payload) => {
      const found = pending.find((p) => p.id === id)
      if (!found) throw new Error(`no session.resume is in flight for ${id}`)
      pending.splice(pending.indexOf(found), 1)
      found.res(payload || { session_id: id, messages: [{ text: id }], info: {} })
      return new Promise((r) => setTimeout(r, 0))
    },
    fail: (id, error) => {
      const found = pending.find((p) => p.id === id)
      if (!found) throw new Error(`no session.resume is in flight for ${id}`)
      pending.splice(pending.indexOf(found), 1)
      found.rej(error || new Error('gone'))
      return new Promise((r) => setTimeout(r, 0))
    },
    settleSub: (id) => {
      const found = subs.find((p) => p.id === id)
      if (!found) throw new Error(`no turn.subscribe is in flight for ${id}`)
      subs.splice(subs.indexOf(found), 1)
      found.res()
      return new Promise((r) => setTimeout(r, 0))
    },
    inFlight: () => pending.map((p) => p.id),
    /* What a rail click does: the row moves the session pointer and THEN asks
       for the conversation (features/rail/RailPage.tsx). Every switch a reader
       makes has this shape, and the pointer moving first is what lets a late
       answer tell that it is no longer the page. */
    click: (row) => { env.sessionSet(row.id); return api.openLiveSession(row) },
  }
}

const painted = (calls) => calls.filter((c) => c[0] === 'renderHistory').map((c) => c[1] && c[1].text)

describe('the assembled live session switch', () => {
  it('paints one conversation when one is opened', async () => {
    const h = harness({ rows: [{ id: 'a' }] })

    h.openLiveSession({ id: 'a', title: 'Alpha' })
    await h.settle('a')

    expect(painted(h.calls)).toEqual(['a'])
    expect(h.env.sessionCurrent()).toBe('a')
    expect(h.title()).toBe('Alpha')
    expect(h.env.live.subId).toBe('sub:a')
    expect(h.calls).toContainEqual(['viewResume', 'a'])
  })

  it('drops the answer to an open the reader has already left', async () => {
    const h = harness({ rows: [{ id: 'a' }, { id: 'b' }] })

    h.click({ id: 'a', title: 'Alpha' })
    h.click({ id: 'b', title: 'Bravo' })
    await h.settle('b')
    await h.settle('a')

    /* Only B's transcript, and only B's -- the defect appended A's messages to
       the lane B had just built, so the reader saw two conversations welded
       together under B's title. */
    expect(painted(h.calls)).toEqual(['b'])
    expect(h.env.sessionCurrent()).toBe('b')
    expect(h.title()).toBe('Bravo')
    expect(h.env.live.subId).toBe('sub:b')
    expect(h.calls.filter((c) => c[0] === 'viewResume')).toEqual([['viewResume', 'b']])
    expect(h.calls.filter((c) => c[0] === 'setCtx')).toHaveLength(1)
  })

  it('drops the answer to an open the reader left for a new task', async () => {
    const h = harness({ rows: [{ id: 'a' }] })

    h.click({ id: 'a', title: 'Alpha' })
    h.startDraft()
    await h.settle('a')

    expect(painted(h.calls)).toEqual([])
    expect(h.env.sessionCurrent()).toBe(null)
    expect(h.title()).toBe('gui.new_task')
    expect(h.state.fresh).toBe('1')
  })

  it('stays quiet when an open the reader left is the one that fails', async () => {
    const h = harness({ rows: [{ id: 'a' }, { id: 'b' }] })

    h.click({ id: 'a', title: 'Alpha' })
    h.click({ id: 'b', title: 'Bravo' })
    await h.settle('b')
    await h.fail('a')

    expect(h.calls.filter((c) => c[0] === 'toast')).toEqual([])
    expect(painted(h.calls)).toEqual(['b'])
    expect(h.state.fresh).toBe(null)
  })

  it('still reports a failure on the open the reader is waiting for', async () => {
    const h = harness({ rows: [{ id: 'a' }] })

    h.openLiveSession({ id: 'a', title: 'Alpha' })
    await h.fail('a')

    expect(h.calls.filter((c) => c[0] === 'toast')).toHaveLength(1)
    expect(h.state.fresh).toBe('1')
  })

  it('leaves the new-task screen when the switch starts, not when it lands', async () => {
    const h = harness({ rows: [{ id: 'a' }] })

    h.openLiveSession({ id: 'a', title: 'Alpha' })

    /* Still waiting on session.resume here. The empty state drives a whole
       layout -- wordmark, crew, a composer 81px above the dock -- so holding it
       across the round trip left it on screen over an empty stage and then
       dropped everything into place at once. */
    expect(h.inFlight()).toEqual(['a'])
    expect(h.state.fresh).toBe(null)
  })

  it('gives the new-task screen back when the reader asks for one', () => {
    const h = harness({ rows: [{ id: 'a' }] })

    h.startDraft()

    expect(h.state.fresh).toBe('1')
  })

  it('takes the visible stream back with a parked turn, without a round trip', async () => {
    const h = harness({ rows: [{ id: 'a' }] })
    h.env.subBySession.a = 'sub:a'
    h.env.parkedTurns.set('a', { nodes: [] })

    h.click({ id: 'a', title: 'Alpha' })

    /* The parked path never reads the transcript back -- the streaming copy is
       the DOM it kept -- so it claims the stream it already has rather than
       subscribing again. */
    expect(h.calls).toContainEqual(['restoreTurn'])
    expect(h.env.live.subId).toBe('sub:a')
    expect(h.calls.filter((c) => c[1] === 'turn.subscribe')).toEqual([])
    expect(h.inFlight()).toEqual([])
  })

  it('does not put a left conversation\'s windows back when its subscription lands late', async () => {
    const h = harness({ rows: [{ id: 'a' }, { id: 'b' }], deferSubscribe: true })

    h.click({ id: 'a', title: 'Alpha' })
    await h.settle('a')
    /* A is drawn and waiting on turn.subscribe. This is the second round trip
       in the same switch, and the reader can leave from here too -- the desk
       replay behind viewResume opens a file window synchronously, so it lands
       on whichever desk is on screen. */
    h.click({ id: 'b', title: 'Bravo' })
    await h.settleSub('a')

    expect(h.calls.filter((c) => c[0] === 'viewResume')).toEqual([])
    expect(h.env.live.subId).toBe(null)
  })

  it('leaves the visible stream alone when a subscription lands too late', async () => {
    const h = harness({ rows: [{ id: 'a' }, { id: 'b' }] })
    h.env.sessionSet('b')
    h.env.live.subId = 'sub:b'

    await h.subscribe('a')

    /* The id is recorded so the parked buffer can still find A's events
       (rpc.notify.event reads subSession), but the visible routing stays with
       the conversation on screen. */
    expect(h.env.subBySession.a).toBe('sub:a')
    expect(h.env.subSession['sub:a']).toBe('a')
    expect(h.env.live.subId).toBe('sub:b')
  })

  it('takes the visible stream for the conversation that is on screen', async () => {
    const h = harness({ rows: [{ id: 'a' }] })
    h.env.sessionSet('a')

    await h.subscribe('a')

    expect(h.env.live.subId).toBe('sub:a')
  })

  it('reuses a subscription the session already has', async () => {
    const h = harness({ rows: [{ id: 'a' }] })
    h.env.sessionSet('a')
    await h.subscribe('a')
    h.env.live.subId = null

    await h.subscribe('a')

    expect(h.env.live.subId).toBe('sub:a')
    expect(h.calls.filter((c) => c[1] === 'turn.subscribe')).toHaveLength(1)
  })

  it('reads the default back when a conversation is left for a new task', async () => {
    /* The other half of the pair: a draft runs the configured default, so
       startDraft has to re-read it or the chip keeps the model of the
       conversation just left. A null session omits the field, which is how the
       default answers. */
    const h = harness({ rows: [{ id: 'a' }] })

    h.openLiveSession({ id: 'a', title: 'Alpha' })
    await h.settle('a')
    h.startDraft()

    const refreshed = h.calls.filter((c) => c[0] === 'loadProviders')
    expect(refreshed.map((c) => c[1])).toEqual(['a', null])
    expect(refreshed.at(-1)[2]).toBeTypeOf('number')
  })

  it('refreshes the model for the conversation being opened, not the one left behind', async () => {
    /* The model is per conversation now. Opening B after A must re-read B's
       binding, or the chip keeps claiming A's model over a conversation that
       runs its own. Keyed to the opened id, so it holds even before the session
       pointer moves over. */
    const h = harness({ rows: [{ id: 'a' }, { id: 'b' }] })

    h.openLiveSession({ id: 'a', title: 'Alpha' })
    await h.settle('a')
    h.openLiveSession({ id: 'b', title: 'Beta' })
    await h.settle('b')

    const refreshed = h.calls.filter((c) => c[0] === 'loadProviders').map((c) => c[1])
    expect(refreshed).toEqual(['a', 'b'])
  })
})
