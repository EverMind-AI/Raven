// @vitest-environment happy-dom
/* Two conversations streaming at once, and what a switch, a background request
 * and a fresh socket do to them.
 *
 * These are the parts of the live smoke list a page can be driven through
 * without a gateway: the frames arrive over the transport, on the subscription
 * the gateway answered with, so the routing under test is the real one --
 * which conversation a frame belongs to, whether that conversation is the one
 * on screen, and what it keeps when it is not.
 */

import { describe, expect, it } from 'vitest'

import { fakeGateway, loadPart, looseQuery } from '../../../scripts/module-harness.mjs'
import * as turn from '../../features/composer/turn'

import type { SessRow } from '../../features/rail/types'
import type { Sources } from '../sources'

interface Row { id: string; title?: string; status?: string | null; last?: string }

async function harness({ rows = [] as Row[] } = {}) {
  const log: unknown[][] = []
  let current: string | null = null
  document.body.innerHTML = '<h1 id="title"></h1><div id="stage"></div><div id="flash"></div>'
  /* The modules under test are imported deepest first, which is the order that
     keeps one module graph: the fakes are installed around the modules the
     first import reaches. */
  await loadPart(async () => {
    await import('./runtime'); await import('./stages')
    await import('./pipeline')
    return import('../../app/install')
  }, {
    fakes: {
      'src/state/caps': { draw: () => {} },
      'src/state/page': { show: () => {} },
      'src/state/ws': {
        draw: () => {},
        setOpen: () => {},
        reset: () => {},
        restore: () => {},
        view: () => ({ tab: 'diff', picked: false }),
        open: false,
      },
      'src/state/session/conversation': {
        ask: (text: string) => log.push(['ask', text]),
        noteRow: () => {},
        pitch: () => {},
        splitAtts: (t: string) => ({ text: t, atts: [] }),
        unpitch: () => {},
      },
      'src/features/rail/store': {
        markNew: () => {},
        draw: () => {},
        endRename: () => {},
        reconcileRows: (_cur: SessRow[], next: SessRow[]) => ({ rows: next, currentMissing: false }),
      },
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
        queueClear: () => {},
        queueRestore: () => {},
        queueShift: () => undefined,
        queueSnapshot: () => [],
        /* The island's own phase machine: "is a turn running" is its answer,
           and the residency rule reads it. */
        turn,
        liveAnchor: () => 0,
        setLiveAnchor: () => {},
        claimDraft: () => {},
      },
      'src/features/composer/approve': {
        openApproval: (_o: unknown, _answer: unknown, owner: string | null) => log.push(['sheet', owner]),
      },
      'src/lib/duration': { formatDuration: (ms: number) => `${ms}ms` },
      'src/i18n/t': { t: (key: string) => key },
      'src/lib/dom': { $: looseQuery() },
      'src/lib/session': {
        current: () => current,
        setCurrent: (id: string | null) => { current = id },
      },
      'src/features/rail/title': { plainTitle: (s: unknown) => String(s) },
      'src/state/toast': { show: (text: string) => log.push(['toast', text]) },
      'src/state/ctxChip': { set: () => {} },
      'src/lib/notifications': { show: () => {} },
      'src/state/banner': { draw: () => {} },
      'src/state/tier': { load: () => {} },
      'src/features/workspace/record': { wsOnHistory: () => {} },
      'src/features/rail/source': { rowPreview: (t: string) => t, touchSession: () => {} },
      'src/features/transcript/source': { renderHistory: () => log.push(['history']) },
      'src/features/transcript/mount': {
        nudge: () => {},
        stopStream: () => {},
        killStatus: () => {},
        step: () => ({
          seal: () => {},
          thinkAppend: () => {},
          sayDelta: (text: string) => log.push(['sayDelta', text]),
          tool: () => ({ done: () => {} }),
        }),
        status: () => {},
      },
      'src/features/transcript/tail': { down: () => {} },
      'src/features/workspace/store': {
        advanceTurn: () => {}, currentTurn: () => 1, loadDeliveries: () => {},
        snapshot: () => ({}), restore: () => {},
      },
      'src/features/desk/store': { claimDraft: () => {} },
      'src/state/session/resume': { resume: () => {}, refreshDag: () => {} },
      'src/features/dag/mount': { forget: () => {} },
    },
  })
  const registry = await import('./registry')
  const pipeline = await import('./pipeline')
  const transport = await fakeGateway((method: string, params: Record<string, string> = {}) => {
    log.push(['rpc', method])
    if (method === 'turn.subscribe') return Promise.resolve({ subscription_id: `sub:${params.session_key}` })
    if (method === 'session.resume') return Promise.resolve({ session_id: params.session_id, messages: [], info: {} })
    return Promise.resolve({})
  })
  const { setSources } = await import('../sources')
  setSources({ composer: { slash: [] }, rail: {}, transcript: {} } as unknown as Partial<Sources>)
  const wiring = await import('../../app/install')
  const connection = await import('../../app/connection')
  wiring.installActions()
  /* The push handlers, which the page's wiring installs beside them. */
  pipeline.installPipeline()
  const tick = () => new Promise((r) => setTimeout(r, 0))
  return {
    registry,
    pipeline,
    transport,
    log,
    rows,
    said: () => log.filter((c) => c[0] === 'sayDelta').map((c) => c[1]),
    /* What a rail click does: the pointer moves and then the conversation is
       asked for. */
    open: async (id: string) => {
      current = id
      await registry.switchTo({ id, title: id } as SessRow)
      await tick()
    },
    /* A frame off the socket, on the subscription the gateway answered with. */
    frame: (key: string, event: unknown) => {
      transport.emit('event', { subscription_id: `sub:${key}`, event })
    },
    onReconnect: [...(connection.reconnectHandlers as Set<() => Promise<void>>)][0]!,
    tick,
  }
}

describe('two conversations streaming at once', () => {
  it('keeps each one\'s tokens to itself', async () => {
    const h = await harness({ rows: [{ id: 'a' }, { id: 'b' }] })
    await h.open('a')
    /* A turn is running on A, which is what a send records. */
    h.frame('a', { type: 'message.start', payload: { turn_id: 't1', content: 'do A' } })
    h.frame('a', { type: 'token.delta', payload: { text: 'from A' } })
    expect(h.said()).toEqual(['from A'])

    /* The reader leaves for B while A is still answering: A holds its turn. */
    await h.open('b')
    expect(h.registry.isResident('a')).toBe(true)

    /* Both keep streaming. A's frames are held; only B's reach the stage. */
    h.frame('a', { type: 'token.delta', payload: { text: ' and more A' } })
    h.frame('b', { type: 'message.start', payload: { turn_id: 't2', content: 'do B' } })
    h.frame('b', { type: 'token.delta', payload: { text: 'from B' } })

    expect(h.said()).toEqual(['from A', 'from B'])
  })

  it('replays what it held when the reader comes back', async () => {
    const h = await harness({ rows: [{ id: 'a' }, { id: 'b' }] })
    await h.open('a')
    h.frame('a', { type: 'message.start', payload: { turn_id: 't1', content: 'do A' } })
    await h.open('b')
    h.frame('a', { type: 'token.delta', payload: { text: ' held' } })

    await h.open('a')

    expect(h.said()).toEqual([' held'])
    /* And it is not holding anything any more: a second return replays nothing. */
    expect(h.registry.isResident('a')).toBe(false)
  })

  it('takes the lane host with the conversation and puts it back', async () => {
    const h = await harness({ rows: [{ id: 'a' }, { id: 'b' }] })
    await h.open('a')
    h.frame('a', { type: 'message.start', payload: { turn_id: 't1', content: 'do A' } })
    /* The transcript island's lane host, which is where the streamed tokens
       live until the turn ends. */
    const host = document.createElement('div')
    host.dataset.tsl = '1'
    host.textContent = 'a half-written answer'
    document.getElementById('stage')!.appendChild(host)

    await h.open('b')

    /* Off the page, and held rather than thrown away -- which is what the
       island asks before it drops a detached one. */
    expect(host.isConnected).toBe(false)
    const hosts = await import('./hosts')
    expect(hosts.holdsHost(host)).toBe(true)

    await h.open('a')

    expect(host.isConnected).toBe(true)
    expect(hosts.holdsHost(host)).toBe(false)
    expect(document.getElementById('stage')!.textContent).toContain('a half-written answer')
  })
})

describe('a request raised on a conversation nobody is looking at', () => {
  it('marks the row asking, because the sheet is on its own screen', async () => {
    const h = await harness({ rows: [{ id: 'a' }, { id: 'b' }] })
    await h.open('b')

    h.transport.emit('approval.request', {
      approval_id: 'ap1', command: 'rm -rf build', conversation_id: 'a',
    })
    await h.tick()

    expect(h.rows[0]!.status).toBe('ask')
    /* The sheet is filed under the conversation that asked, not the one open. */
    expect(h.log).toContainEqual(['sheet', 'a'])
  })
})

describe('a fresh socket', () => {
  it('drops what every conversation was holding and reads the open one back', async () => {
    const h = await harness({ rows: [{ id: 'a' }, { id: 'b' }] })
    await h.open('a')
    h.frame('a', { type: 'message.start', payload: { turn_id: 't1', content: 'do A' } })
    await h.open('b')
    h.frame('a', { type: 'token.delta', payload: { text: ' held' } })
    expect(h.registry.isResident('a')).toBe(true)

    const done = h.onReconnect()
    await h.tick()
    await done

    /* The frames a held turn missed while the socket was down are gone, so the
       copies go with them and a re-open rebuilds from disk. */
    expect(h.registry.isResident('a')).toBe(false)
    expect(h.registry.bySubscription('sub:a')).toBeUndefined()
    /* And the reader is told the page is back. */
    expect(h.log).toContainEqual(['rpc', 'system.hello'])
  })
})
