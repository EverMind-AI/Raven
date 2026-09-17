// @vitest-environment happy-dom
/* Turning the draft on screen into a real conversation.
 *
 * Two callers depend on this and only one of them is a send: the composer's
 * first message, and the sub-agent roster's new-instance button, which needs a
 * conversation for the instance to live in. The part is driven as a module,
 * with its collaborators replaced per part and per export
 * (ui-web/scripts/module-harness.mjs) -- so the draft is entered through the real
 * `startDraft` and the promotion is the real one.
 *
 * What is pinned here is the ORDER, which is the part a reader cannot see and
 * the send silently depends on: the pointer moves, then the caller's hook runs,
 * then the staged settings go up. The hook is where `liveSend` records the
 * turn's owner, and a reader switching conversations inside those round trips
 * would otherwise leave the in-flight turn parked under the wrong one. The
 * order is read off the traffic the promotion actually puts on the transport
 * rather than off injected functions: the staged model, tier and permission
 * writes ARE three calls, and a test that watched stand-ins for them could not
 * tell a call that was made from one that was only prepared.
 */

import { describe, expect, it } from 'vitest'

import { fakeGateway, loadPart, looseQuery } from '../../../scripts/module-harness.mjs'

type Runtime = typeof import('./runtime')
type Registry = typeof import('./registry')

interface Row { id: string; title: string; last: string; persisted: boolean }
interface Staged { model: { model: string; provider: string } | null; tier: string | null; perm: string | null }

/* What a staged pick writes, named by the call it becomes: `config.set` carries
   both the model and the permission mode, so the key is part of the name. */
const traffic = (method: string, params: { key?: string }): string =>
  `rpc:${method}${method === 'config.set' ? `:${params.key ?? ''}` : ''}`

async function harness(startAsDraft: boolean, { refuseModelWrite = false } = {}) {
  const log: string[] = []
  const rows: Row[] = []
  /* Every write to the staged model the promotion refuses and re-reads, with
     the generation it was checked against -- the one observable that says which
     view the promotion belongs to. */
  const reReads: Array<[string | null, number]> = []
  let current: string | null = startAsDraft ? null : 'already-open'
  let minted = 0
  let bumpOnCreate: (() => void) | null = null

  /* The runtime is imported first and the part that wires it after, which is
     the order that keeps one module graph: the fakes are installed around the
     modules the first import reaches, and one it did not is loaded afterwards
     without them. */
  await loadPart(async () => { await import('./runtime'); return import('../../app/install') }, {
    fakes: {
      'src/state/caps': { draw: () => {} },
      'src/state/page': { show: () => {} },
      'src/state/ws': { setOpen: () => {}, reset: () => {} },
      'src/state/session/conversation': {
        ask: () => {},
        pitch: () => {},
        splitAtts: (t: string) => ({ text: t, atts: [] }),
        unpitch: () => {},
      },
      'src/state/session/rows': {
        open: () => {},
        replace: () => {},
        rows: () => rows,
        sess: (id: string) => rows.find((r) => r.id === id),
      },
      'src/features/rail/store': { markNew: () => {}, draw: () => log.push('draw'), endRename: () => {} },
      'src/state/sheetRack': { forget: () => {} },
      'src/features/composer/mount': {
        drawMeter: () => {},
        goPaint: () => {},
        dropDraft: () => {},
        loadDraft: () => {},
        parkDraft: () => {},
        queueClear: () => {},
        queuePush: () => {},
        queueShift: () => null,
        turn: { dispatch: () => {}, busy: () => false, snapshot: () => ({}), restore: () => {} },
        claimDraft: (id: string | null) => log.push(`claimDraft:${String(id)}`),
      },
      'src/i18n/t': { T: (key: string) => key },
      'src/lib/dom': { $: looseQuery() },
      'src/state/banner': { draw: () => {} },
      'src/state/tier': { load: () => {} },
      'src/lib/session': { current: () => current, setCurrent: (id: string | null) => { current = id; log.push(`pointer:${String(id)}`) } },
      'src/state/toast': { show: (text: string) => log.push(`toast:${text}`) },
      'src/features/workspace/record': { wsOnHistory: () => {} },
      'src/features/rail/source': { rowPreview: (t: string) => t, touchSession: (id: string) => log.push(`touch:${id}`) },
      'src/state/session/residency': { park: () => {}, resume: () => {} },
      'src/features/model/source': {
        /* The re-read a refused model write ends with, which is the only place
           the generation the promotion began on becomes visible. */
        loadProviders: (id: string | null, gen: number) => { reReads.push([id, gen]) },
        openModelsForMissingProvider: () => false,
        stagedTier: () => staging().tier,
      },
      'src/features/settings/source': {
        loadPermMode: () => {},
        stagedPerm: () => staging().perm,
      },
      'src/features/workspace/source': { wsSetRoot: (root: string) => log.push(`wsRoot:${root}`) },
      'src/features/transcript/mount': {
        killStatus: () => {},
        status: () => {},
      },
    },
  })
  const runtime = (await import('./runtime')) as Runtime
  const registry = (await import('./registry')) as Registry
  /* The view ticket is its own module now; taken from the instance loadPart's
     reset just produced, not from a binding held across it. */
  const { generation } = await import('./generation')
  const { staging } = await import('./staging')

  await fakeGateway(async (method: string, params: { key?: string } = {}) => {
    log.push(traffic(method, params))
    if (method === 'session.create') {
      minted += 1
      if (bumpOnCreate) bumpOnCreate()
      return { session_id: `made-${minted}`, info: { cwd: '/w' } }
    }
    if (method === 'config.set' && params.key === 'model' && refuseModelWrite) {
      throw new Error('refused')
    }
    return {}
  })

  /* The real way in: `startDraft` is what raises the flag `openConversation`
     reads, and it takes the next view ticket while it does. Its own wiring is
     noise here, so the log starts after it. */
  const enterDraft = (): void => { registry.switchToDraft(); log.length = 0 }
  if (startAsDraft) enterDraft()
  /* Staged picks belong to the draft, so they go on after it: startDraft
     clears all three. */
  const stage = (over: Partial<Staged> = {}): void => {
    Object.assign(staging(), { model: { model: 'm', provider: 'p' }, tier: 'high', perm: 'ask', ...over })
  }

  return {
    openConversation: runtime.openConversation,
    sendOnSession: runtime.sendOnSession,
    isDraft: () => runtime.isDraft(),
    viewGen: () => generation(),
    enterDraft,
    stage,
    onCreate: (fn: () => void) => { bumpOnCreate = fn },
    /* Counted on the transport rather than in the log, which a new draft
       clears. */
    minted: () => minted,
    reReads,
    log,
    rows,
    pointer: () => current,
  }
}

describe('getting a conversation to work in', () => {
  it('answers the open one, and makes nothing, when there already is one', async () => {
    /* "Give me a conversation" is what both callers want, so the seam answers it
       rather than having to be asked separately whether it applies. */
    const h = await harness(false)
    await expect(h.openConversation()).resolves.toBe('already-open')
    expect(h.minted()).toBe(0)
    expect(h.log).toEqual([])
    expect(h.rows).toEqual([])
  })

  it('promotes the draft, and answers with the conversation it made', async () => {
    const h = await harness(true)
    await expect(h.openConversation()).resolves.toBe('made-1')
    expect(h.isDraft()).toBe(false)
    expect(h.pointer()).toBe('made-1')
    /* On the rail, unsaved, and carrying the "not started" line a conversation
       with no message has -- this caller has no message to preview. */
    expect(h.rows).toHaveLength(1)
    expect(h.rows[0]!.id).toBe('made-1')
    expect(h.rows[0]!.last).toBe('gui.sess.not_started')
    expect(h.rows[0]!.persisted).toBe(false)
  })

  it('takes the caller preview for the row when there is one', async () => {
    /* The send's half: its row says what was asked, not that nothing was. */
    const h = await harness(true)
    await h.openConversation('do the thing')
    expect(h.rows[0]!.last).toBe('do the thing')
  })

  it('runs the caller hook once the pointer has moved and before the settings go up', async () => {
    const h = await harness(true)
    h.stage()
    const seen: Array<string | null> = []
    await h.openConversation(undefined, (id) => {
      h.log.push(`hook:${id}`)
      seen.push(h.pointer())
    })
    /* The hook sees the NEW conversation, which is the whole reason it is called
       from inside rather than awaited outside. */
    expect(seen).toEqual(['made-1'])
    expect(h.log).toEqual([
      'rpc:session.create',
      'wsRoot:/w',
      'pointer:made-1',
      'hook:made-1',
      'claimDraft:made-1',
      'rpc:config.set:model',
      'rpc:session.set_mode',
      'rpc:config.set:permissions.mode',
      'draw',
      'rpc:turn.subscribe',
    ])
  })

  it('mints one conversation for two callers that land inside the same promotion', async () => {
    /* `draft` stays raised across `session.create`, so a second press arriving in
       that window used to read the page as still a draft. Both callers want the
       same conversation; both get it, and each one's hook still runs. */
    const h = await harness(true)
    const hooks: string[] = []
    const [a, b] = await Promise.all([
      h.openConversation('first', (id) => hooks.push(`a:${id}`)),
      h.openConversation('second', (id) => hooks.push(`b:${id}`)),
    ])
    expect([a, b]).toEqual(['made-1', 'made-1'])
    expect(h.minted()).toBe(1)
    expect(h.rows).toHaveLength(1)
    expect(hooks.sort()).toEqual(['a:made-1', 'b:made-1'])
  })

  it('promotes the NEXT draft too, rather than answering with the last one', async () => {
    /* The hold is released when the promotion ends, not kept for the life of the
       page: the reader goes back to the new-task screen -- `startDraft` raises
       the flag again -- and that draft has to become its own conversation. A
       hold left standing would hand it the previous one, and the instance would
       be filed under a conversation the reader had left. */
    const h = await harness(true)
    await expect(h.openConversation()).resolves.toBe('made-1')
    h.enterDraft()
    await expect(h.openConversation()).resolves.toBe('made-2')
    expect(h.minted()).toBe(2)
    expect(h.rows.map((r) => r.id)).toEqual(['made-2', 'made-1'])
  })

  it('holds a settled send behind a promotion the roster started', async () => {
    /* `promote` moves the pointer and lowers `draft` BEFORE awaiting the staged
       model, tier and permission writes and the subscription. A send landing in
       that window reads the page as a settled conversation and dispatches at
       once -- so the first turn starts on a conversation whose draft-selected
       settings are not on it yet and whose events have nowhere to arrive.

       Reachable only since the roster gained the ability to promote. Every
       other way in went through `liveSend`, which marks the turn busy before
       the promotion starts, so a second send was queued rather than sent. */
    const h = await harness(true)
    h.stage()
    const rosterDone = h.openConversation()
    /* Let the create resolve, which is what moves the pointer and lowers the
       flag -- the window the earlier concurrency test never entered, because it
       joined while `session.create` was still pending. */
    await Promise.resolve()
    await Promise.resolve()
    expect(h.isDraft()).toBe(false)
    h.sendOnSession('hello', () => {})
    await rosterDone
    await Promise.resolve()
    const sent = h.log.indexOf('rpc:turn.send')
    expect(sent).toBeGreaterThan(-1)
    /* After all four, not before any of them. */
    for (const step of ['rpc:config.set:model', 'rpc:session.set_mode', 'rpc:config.set:permissions.mode', 'rpc:turn.subscribe']) {
      expect(h.log.indexOf(step)).toBeGreaterThan(-1)
      expect(sent).toBeGreaterThan(h.log.indexOf(step))
    }
  })

  it('carries the view generation the promotion began on into the staged model', async () => {
    /* Taken before the first await, so a late answer is checked against the view
       as it stood when this started rather than whatever is open by then. Made
       visible by refusing the write: a refusal re-reads the providers against
       the generation it carried. The reader leaves for a new draft while
       `session.create` is in flight -- which is what raises the ticket -- and
       the staged pick is put back, so the write still happens. */
    const h = await harness(true, { refuseModelWrite: true })
    h.stage()
    const began = h.viewGen()
    h.onCreate(() => { h.enterDraft(); h.stage() })
    await h.openConversation()
    expect(h.viewGen()).toBe(began + 1)
    expect(h.reReads).toContainEqual(['made-1', began])
  })
})
