// @vitest-environment happy-dom
/* The registry's session switch: what a round trip is allowed to paint once
 * the reader has moved on, and when the new-task screen comes down.
 *
 * Driven through the real switch rather than through assertions about its
 * source, because the defect this pins is a matter of ordering between two
 * in-flight opens -- nothing about the text of either function says which of
 * them wins.
 */

import { describe, expect, it } from 'vitest'

import { fakeGateway, loadPart } from '../../../scripts/module-harness.mjs'

import type { SessRow } from '../../features/rail/types'

type Registry = typeof import('./registry')
type Wiring = typeof import('../install')

interface Row { id: string; title?: string; status?: string | null }
interface Staged { model: unknown; tier: string | null; perm: string | null }

async function harness({ rows, deferSubscribe }: { rows?: Row[]; deferSubscribe?: boolean } = {}) {
  const calls: unknown[][] = []
  document.body.innerHTML = '<h1 id="title"></h1><div id="stage"></div><div id="flash"></div>'
  const boxes: Record<string, HTMLElement | null | undefined> = {}
  for (const id of ['#title', '#stage', '#flash']) boxes[id] = document.getElementById(id.slice(1))
  /* The top-bar editor stands IN PLACE OF h1#title, so while it is open that id
     resolves to nothing; committing puts the heading back and writes the typed
     name onto the row the editor captured when it opened. */
  const editor = { commit: () => {} }
  const spare = new Map<string, HTMLElement>()
  const $ = (selector: string): HTMLElement | null => {
    if (selector in boxes || selector === '#title') return boxes[selector] || null
    /* Everything install() wires that this file is not about. */
    if (!spare.has(selector)) spare.set(selector, document.createElement('div'))
    return spare.get(selector) as HTMLElement
  }
  let current: string | null = null
  const state: { fresh: string | null } = { fresh: '1' }
  const pending: Array<{ id: string; res: (v: unknown) => void; rej: (e: unknown) => void }> = []
  const subs: Array<{ id: string; res: () => void }> = []
  /* The seam the registry reaches its own switch through: sessionOpen is
     sources.sessions.open, which the boot installs as the switch. Bound
     through this holder rather than stubbed, so the reconnect drives the real
     function. */
  const api: { switchTo?: Registry['switchTo'] } = {}
  /* The registry is imported first and the module that wires it after, which
     is the order that keeps one module graph: the fakes are installed around
     the modules the first import reaches, and one it did not is loaded
     afterwards without them. */
  await loadPart(async () => { await import('./registry'); return import('../install') }, {
    fakes: {
      'src/state/caps': { draw: () => {} },
      'src/state/page': { show: () => {} },
      'src/state/ws': { setOpen: () => {}, reset: () => {} },
      'src/state/session/conversation': {
        pitch: () => { state.fresh = '1'; calls.push(['pitch']) },
        unpitch: () => { state.fresh = null; calls.push(['unpitch']) },
      },
      'src/features/rail/store': { markNew: () => {}, draw: () => calls.push(['sessionDraw']) },
      'src/state/session/rows': {
        sess: (id: string) => (rows || []).find((r) => r.id === id),
        open: (s: Row) => api.switchTo!(s as SessRow),
        replace: () => {},
        rows: () => rows || [],
      },
      'src/features/composer/mount': {
        drawMeter: () => {},
        goPaint: () => {},
        loadDraft: (id: string) => calls.push(['loadDraft', id]),
        parkDraft: () => {},
        queueClear: () => {},
        queueRestore: () => {},
        queueShift: () => undefined,
        turn: {
          dispatch: () => {}, busy: () => false, snapshot: () => ({}),
          restore: () => {}, reduce: (phase: unknown) => phase,
        },
      },
      'src/i18n/t': { T: (key: string) => key },
      'src/shell/dom': { $ },
      'src/shell/session': { current: () => current, setCurrent: (id: string | null) => { current = id; calls.push(['sessionSet', id]) } },
      'src/features/rail/title': { plainTitle: (s: unknown) => String(s) },
      'src/shell/toast': { show: (text: string) => calls.push(['toast', text]) },
      'src/shell/ctxchip': { set: (used: number) => calls.push(['setCtx', used]) },
      'src/shell/banner': { draw: () => {} },
      'src/shell/tier': { load: () => calls.push(['loadTier']) },
      'src/shell/perm': { setFromConfig: () => {} },
      'src/features/workspace/record': { wsOnHistory: () => {} },
      'src/features/transcript/source': {
        renderHistory: (messages: Array<{ text: string }>) => { state.fresh = null; calls.push(['renderHistory', messages[0]]) },
      },
      'src/state/session/residency': {
        park: () => calls.push(['parkTurn']),
        resume: () => calls.push(['restoreTurn']),
      },
      'src/features/workspace/source': { wsSetRoot: (root: string) => calls.push(['wsSetRoot', root]) },
    },
    islands: {
      /* The streaming buffer the turn state resets through. */
      transcript: {
        nudge: () => {},
        stopStream: () => {},
        killStatus: () => {},
        status: (text: string) => calls.push(['showStatus', text]),
      },
      workspace: { loadDeliveries: (id: string) => calls.push(['loadDeliveries', id]) },
      view: {
        resume: (id: string) => calls.push(['viewResume', id]),
        refreshDag: (id: string) => calls.push(['viewRefreshDag', id]),
      },
      /* What the heading held when the editor was ended, so the order is
         assertable: ending it has to come before anything reads or writes
         h1#title, and while one is open that id resolves to nothing. */
      rail: {
        endRename: () => {
          calls.push(['endRename', boxes['#title'] ? boxes['#title']!.textContent : null])
          editor.commit()
        },
      },
    },
  })
  const registry = (await import('./registry')) as Registry
  api.switchTo = registry.switchTo
  await fakeGateway((method: string, params: Record<string, string> = {}) => {
    calls.push(['rpc', method, params || {}])
    if (method === 'session.resume') {
      return new Promise((res, rej) => pending.push({ id: params.session_id!, res, rej }))
    }
    if (method === 'turn.subscribe') {
      const answer = { subscription_id: `sub:${params.session_key}` }
      if (!deferSubscribe) return Promise.resolve(answer)
      return new Promise((res) => subs.push({ id: params.session_key!, res: () => res(answer) }))
    }
    /* The model and permission refreshes a switch starts are the real ones
       here, and an empty envelope is a valid answer to both. */
    return Promise.resolve({})
  })
  const wiring = (await import('../install')) as Wiring
  const { setSources } = await import('../sources')
  setSources({ composer: { slash: [] }, sessions: {}, transcript: {} } as never)
  const { staging } = await import('./staging')
  const connection = await import('../connection')
  wiring.installActions()
  /* The four page-level names the switch used to keep, as the conversations
     that keep them now: the subscription whose frames paint the stage is the
     one on the conversation being shown, the two books are that same field
     read either way round, and the staged picks belong to the draft. */
  const sub = (key: string | null) => registry.get(key)?.subscriptionId ?? null
  const env = {
    live: {
      get subId() { return sub(current) },
      set subId(id: string | null) { registry.record(current, id) },
    },
    subBySession: new Proxy({} as Record<string, string>, {
      get: (_t, key: string) => sub(key),
      set: (_t, key: string, id: string) => { registry.record(key, id); return true },
    }),
    subSession: new Proxy({} as Record<string, string>, {
      get: (_t, id: string) => registry.bySubscription(id),
    }),
    parkedTurns: { set: (key: string, _pk: unknown) => { registry.ensure(key).events = [] } },
    get staged() { return staging() as Staged },
    sessionCurrent: () => current,
    sessionSet: (id: string | null) => { current = id; calls.push(['sessionSet', id]) },
    sess: (id: string) => (rows || []).find((r) => r.id === id),
  }
  const asked = (method: string) => calls.filter((c) => c[0] === 'rpc' && c[1] === method).map((c) => c[2])
  return {
    subscribe: registry.subscribe,
    startDraft: registry.switchToDraft,
    openLiveSession: (row: Row) => registry.switchTo(row as SessRow),
    /* The handler state/connection.ts runs once the transport says the socket
       is back. Registered by installActions(), one registrar today. */
    onReconnect: [...(connection.reconnectHandlers as Set<() => Promise<void>>)][0]!,
    calls,
    env,
    state,
    asked,
    title: () => (boxes['#title'] ? boxes['#title']!.textContent : null),
    openEditor: (typed: string) => {
      const heading = boxes['#title']
      const captured = (rows || []).find((r) => r.id === current)
      delete boxes['#title']
      editor.commit = () => {
        boxes['#title'] = heading
        if (captured) captured.title = typed
        editor.commit = () => {}
      }
    },
    settle: (id: string, payload?: unknown) => {
      const found = pending.find((p) => p.id === id)
      if (!found) throw new Error(`no session.resume is in flight for ${id}`)
      pending.splice(pending.indexOf(found), 1)
      found.res(payload || { session_id: id, messages: [{ text: id }], info: {} })
      return new Promise((r) => setTimeout(r, 0))
    },
    fail: (id: string, error?: Error) => {
      const found = pending.find((p) => p.id === id)
      if (!found) throw new Error(`no session.resume is in flight for ${id}`)
      pending.splice(pending.indexOf(found), 1)
      found.rej(error || new Error('gone'))
      return new Promise((r) => setTimeout(r, 0))
    },
    settleSub: (id: string) => {
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
    click: (row: Row) => { env.sessionSet(row.id); return registry.switchTo(row as SessRow) },
  }
}

const painted = (calls: unknown[][]) =>
  calls.filter((c) => c[0] === 'renderHistory').map((c) => (c[1] as { text: string } | undefined)?.text)

describe('the live session switch', () => {
  it('paints one conversation when one is opened', async () => {
    const h = await harness({ rows: [{ id: 'a' }] })

    h.openLiveSession({ id: 'a', title: 'Alpha' })
    await h.settle('a')

    expect(painted(h.calls)).toEqual(['a'])
    expect(h.env.sessionCurrent()).toBe('a')
    expect(h.title()).toBe('Alpha')
    expect(h.env.live.subId).toBe('sub:a')
    expect(h.calls).toContainEqual(['viewResume', 'a'])
  })

  it('drops the answer to an open the reader has already left', async () => {
    const h = await harness({ rows: [{ id: 'a' }, { id: 'b' }] })

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

  /* The title editor stands IN PLACE OF h1#title, so while one is open that id
     resolves to nothing -- and both switch paths write the heading, while the
     reconnect that reloads the open conversation used to READ it. With an
     editor up that read threw, which left the input in the top bar for the
     life of the tab and the conversation never reloaded, never re-subscribed
     and never told the reader it was back.

     Ordering is the whole point, so the stub records what the heading held at
     the moment it was ended: the name being left behind, never the one being
     written. */
  it('ends an open title editor before either path reaches the heading', async () => {
    const h = await harness({ rows: [{ id: 'a' }] })

    h.openLiveSession({ id: 'a', title: 'Alpha' })
    await h.settle('a')
    expect(h.calls).toContainEqual(['endRename', ''])
    expect(h.title()).toBe('Alpha')

    h.startDraft()
    expect(h.calls).toContainEqual(['endRename', 'Alpha'])
    expect(h.title()).toBe('gui.new_task')
  })

  /* The reconnect is the one caller that builds a DETACHED { id, title } for
     the switch; every other one hands over the live row, whose title the
     commit mutates in place. So it is the only one that can read a title, have
     the editor commit a different one underneath it, and then paint the value it
     captured -- which is what happened, because the teardown lives inside the
     switch and ran after the lookup here.

     Driven through the real reconnect handler rather than the switch: the
     ordering is between the two, so a test that calls the inner one cannot see
     it. */
  it('paints the title the editor committed, not the one it read first', async () => {
    const h = await harness({ rows: [{ id: 'a', title: 'Alpha' }] })
    h.openLiveSession({ id: 'a', title: 'Alpha' })
    await h.settle('a')
    expect(h.title()).toBe('Alpha')

    h.openEditor('reconciliation audit')
    expect(h.title()).toBe(null)

    const reconnected = h.onReconnect()
    /* system.hello is awaited before the reload, so let that microtask land or
       there is no session.resume in flight yet. */
    await new Promise((r) => setTimeout(r, 0))
    await h.settle('a')
    await reconnected

    expect(h.calls).toContainEqual(['endRename', null])
    expect(h.title()).toBe('reconciliation audit')
    expect(h.env.sess('a')!.title).toBe('reconciliation audit')
  })

  it('drops the answer to an open the reader left for a new task', async () => {
    const h = await harness({ rows: [{ id: 'a' }] })

    h.click({ id: 'a', title: 'Alpha' })
    h.startDraft()
    await h.settle('a')

    expect(painted(h.calls)).toEqual([])
    expect(h.env.sessionCurrent()).toBe(null)
    expect(h.title()).toBe('gui.new_task')
    expect(h.state.fresh).toBe('1')
  })

  it('stays quiet when an open the reader left is the one that fails', async () => {
    const h = await harness({ rows: [{ id: 'a' }, { id: 'b' }] })

    h.click({ id: 'a', title: 'Alpha' })
    h.click({ id: 'b', title: 'Bravo' })
    await h.settle('b')
    await h.fail('a')

    expect(h.calls.filter((c) => c[0] === 'toast')).toEqual([])
    expect(painted(h.calls)).toEqual(['b'])
    expect(h.state.fresh).toBe(null)
  })

  it('still reports a failure on the open the reader is waiting for', async () => {
    const h = await harness({ rows: [{ id: 'a' }] })

    h.openLiveSession({ id: 'a', title: 'Alpha' })
    await h.fail('a')

    expect(h.calls.filter((c) => c[0] === 'toast')).toHaveLength(1)
    expect(h.state.fresh).toBe('1')
  })

  it('leaves the new-task screen when the switch starts, not when it lands', async () => {
    const h = await harness({ rows: [{ id: 'a' }] })

    h.openLiveSession({ id: 'a', title: 'Alpha' })

    /* Still waiting on session.resume here. The empty state drives a whole
       layout -- wordmark, crew, a composer 81px above the dock -- so holding it
       across the round trip left it on screen over an empty stage and then
       dropped everything into place at once. */
    expect(h.inFlight()).toEqual(['a'])
    expect(h.state.fresh).toBe(null)
  })

  it('gives the new-task screen back when the reader asks for one', async () => {
    const h = await harness({ rows: [{ id: 'a' }] })

    h.startDraft()

    expect(h.state.fresh).toBe('1')
  })

  it('takes the visible stream back with a parked turn, without a round trip', async () => {
    const h = await harness({ rows: [{ id: 'a' }] })
    h.env.subBySession.a = 'sub:a'
    h.env.parkedTurns.set('a', { nodes: [] })

    /* Awaited, where the tests above settle a round trip instead: this path
       makes none, but it does claim the stream behind an await, and the graph
       re-read lands on the far side of it. */
    await h.click({ id: 'a', title: 'Alpha' })

    /* The parked path never reads the transcript back -- the streaming copy is
       the DOM it kept -- so it claims the stream it already has rather than
       subscribing again. */
    expect(h.calls).toContainEqual(['restoreTurn'])
    expect(h.env.live.subId).toBe('sub:a')
    expect(h.asked('turn.subscribe')).toEqual([])
    expect(h.inFlight()).toEqual([])
    /* The graph half of the replay and not the desk half. A parked turn buffers
       the events it misses only while it is busy, and a graph outlives the turn
       that started it, so the sheet on screen is stale and has to be re-read --
       while the windows never left the page and replaying their opens would give
       the reader each one twice. */
    expect(h.calls).toContainEqual(['viewRefreshDag', 'a'])
    expect(h.calls.filter((c) => c[0] === 'viewResume')).toEqual([])
  })

  it('does not put a left conversation\'s windows back when its subscription lands late', async () => {
    const h = await harness({ rows: [{ id: 'a' }, { id: 'b' }], deferSubscribe: true })

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
    const h = await harness({ rows: [{ id: 'a' }, { id: 'b' }] })
    h.env.sessionSet('b')
    h.env.live.subId = 'sub:b'

    await h.subscribe('a')

    /* The id is recorded so the parked buffer can still find A's events
       (the event handler reads subSession), but the visible routing stays with
       the conversation on screen. */
    expect(h.env.subBySession.a).toBe('sub:a')
    expect(h.env.subSession['sub:a']).toBe('a')
    expect(h.env.live.subId).toBe('sub:b')
  })

  it('takes the visible stream for the conversation that is on screen', async () => {
    const h = await harness({ rows: [{ id: 'a' }] })
    h.env.sessionSet('a')

    await h.subscribe('a')

    expect(h.env.live.subId).toBe('sub:a')
  })

  it('reuses a subscription the session already has', async () => {
    const h = await harness({ rows: [{ id: 'a' }] })
    h.env.sessionSet('a')
    await h.subscribe('a')
    /* Nothing to release: the subscription whose frames paint the stage is not
       a second field any more, it is the conversation's own. */

    await h.subscribe('a')

    expect(h.env.live.subId).toBe('sub:a')
    expect(h.asked('turn.subscribe')).toHaveLength(1)
  })

  it('reads the default back when a conversation is left for a new task', async () => {
    /* The other half of the pair: a draft runs the configured default, so
       startDraft has to re-read it or the chip keeps the model of the
       conversation just left. A null session omits the field, which is how the
       default answers. */
    const h = await harness({ rows: [{ id: 'a' }] })

    h.openLiveSession({ id: 'a', title: 'Alpha' })
    await h.settle('a')
    h.startDraft()

    expect(h.asked('model.options')).toEqual([{ session_id: 'a' }, {}])
  })

  it('drops a tier staged for a draft that was abandoned', async () => {
    /* The pick was for the conversation the reader was writing, and they left it
       without sending. Kept, it is spent by whichever conversation is sent next
       -- an invisible choice crossing from one conversation to another. Reset on
       both paths out of a draft, exactly where the staged model is. */
    const h = await harness({ rows: [{ id: 'a' }] })

    h.env.staged.tier = 'max'
    h.openLiveSession({ id: 'a', title: 'Alpha' })
    await h.settle('a')
    expect(h.env.staged.tier).toBeNull()

    h.env.staged.tier = 'max'
    h.startDraft()
    expect(h.env.staged.tier).toBeNull()
  })

  it('re-reads the tier when the same conversation is reopened', async () => {
    /* The reconnect path reopens the CURRENT id, and the tier used to be read
       from `session.onChange` -- which `setCurrent` never fires for an id that
       has not changed. The loop holds session policies in memory with no
       persistence, so a gateway restart puts every session back on the
       catalogue default while the chip went on naming the tier from before. */
    const h = await harness({ rows: [{ id: 'a' }] })

    h.openLiveSession({ id: 'a', title: 'Alpha' })
    await h.settle('a')
    h.openLiveSession({ id: 'a', title: 'Alpha' })
    await h.settle('a')

    expect(h.calls.filter((c) => c[0] === 'loadTier')).toHaveLength(2)
  })

  it('reads the tier back when a conversation is left for a new task', async () => {
    const h = await harness({ rows: [{ id: 'a' }] })

    h.openLiveSession({ id: 'a', title: 'Alpha' })
    await h.settle('a')
    h.startDraft()

    expect(h.calls.filter((c) => c[0] === 'loadTier')).toHaveLength(2)
  })

  it('holds a staged permission mode to the tier\'s rules: reset on both paths, re-read per open', async () => {
    const h = await harness({ rows: [{ id: 'a' }] })

    h.env.staged.perm = 'full'
    h.openLiveSession({ id: 'a', title: 'Alpha' })
    await h.settle('a')
    expect(h.env.staged.perm).toBeNull()
    h.env.staged.perm = 'full'
    h.startDraft()
    expect(h.env.staged.perm).toBeNull()

    /* Keyed to the opened id, then to no id at all: a draft runs the default. */
    expect(h.asked('config.get').map((p) => (p as { session_id?: string }).session_id ?? null)).toEqual(['a', null])
  })

  it('refreshes the model for the conversation being opened, not the one left behind', async () => {
    /* The model is per conversation now. Opening B after A must re-read B's
       binding, or the chip keeps claiming A's model over a conversation that
       runs its own. Keyed to the opened id, so it holds even before the session
       pointer moves over. */
    const h = await harness({ rows: [{ id: 'a' }, { id: 'b' }] })

    h.openLiveSession({ id: 'a', title: 'Alpha' })
    await h.settle('a')
    h.openLiveSession({ id: 'b', title: 'Beta' })
    await h.settle('b')

    expect(h.asked('model.options').map((p) => (p as { session_id?: string }).session_id)).toEqual(['a', 'b'])
  })
})
