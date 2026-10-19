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

interface Row { id: string; title: string; last: string; persisted: boolean; workdir?: string | null }
interface Staged { model: { model: string; provider: string } | null; tier: string | null; perm: string | null }

/* What a staged pick writes, named by the call it becomes: `config.set` carries
   both the model and the permission mode, so the key is part of the name. */
const traffic = (method: string, params: { key?: string }): string =>
  `rpc:${method}${method === 'config.set' ? `:${params.key ?? ''}` : ''}`

/* `realModel` keeps the model chip's own source and store instead of the stand-in
   below, so a case can read what the chip says across a promotion; the transport
   then answers `model.options` itself, at once or when a case releases it. */
async function harness(
  startAsDraft: boolean,
  { refuseModelWrite = false, holdTurnSend = false, realModel = false, selectionOnly = true } = {},
) {
  const log: string[] = []
  /* The reload a refused model write fires, held until a case lands it: on a
     real page it is a round trip, and what happens before it lands is the
     window under test. */
  const reloads: Array<() => void> = []
  let releaseSend: () => void = () => {}
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
      'src/i18n/t': { t: (key: string) => key },
      'src/lib/dom': { $: looseQuery() },
      'src/state/banner': { draw: () => {} },
      'src/state/tier': { load: () => {} },
      'src/lib/session': { current: () => current, setCurrent: (id: string | null) => { current = id; log.push(`pointer:${String(id)}`) } },
      'src/state/toast': { show: (text: string) => log.push(`toast:${text}`) },
      'src/features/workspace/record': { wsOnHistory: () => {} },
      'src/features/rail/source': { rowPreview: (t: string) => t, touchSession: (id: string) => log.push(`touch:${id}`) },
      'src/state/session/residency': { park: () => {}, resume: () => {} },
      ...(realModel ? {} : {
        'src/features/model/source': {
          /* The re-read a refused model write ends with, which is the only place
             the generation the promotion began on becomes visible. */
          loadProviders: (id: string | null, gen: number) => {
            reReads.push([id, gen])
            return new Promise<void>((done) => { reloads.push(done) })
          },
          openModelsForMissingProvider: () => false,
          stagedTier: () => staging().tier,
        },
      }),
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
  /* The draft's working directory is its own module (state/workdir.ts), for
     the reason its header gives; the promotion reads it the same way. */
  const workdir = await import('../workdir')

  const created: object[] = []
  /* `model.options` as the real chip asks it: answered with `answer` while
     `holdOptions` is off, otherwise parked until `answerHeld` or `failHeld`. */
  const options = {
    answer: 'draft-m',
    holdOptions: false,
    asked: [] as object[],
    held: [] as Array<{ resolve: (v: object) => void; reject: (e: Error) => void }>,
  }
  if (realModel) {
    const { absorb } = await import('../../rpc/capabilities')
    absorb(selectionOnly ? ['model.options.selection_only'] : [])
  }
  await fakeGateway(async (method: string, params: { key?: string } = {}) => {
    if (method === 'model.options') {
      options.asked.push(params)
      if (!options.holdOptions) return { model: options.answer, provider: 'p', providers: [] }
      return new Promise<object>((resolve, reject) => { options.held.push({ resolve, reject }) })
    }
    log.push(traffic(method, params))
    if (method === 'session.create') {
      created.push(params)
      minted += 1
      if (bumpOnCreate) bumpOnCreate()
      return { session_id: `made-${minted}`, info: { cwd: '/w' } }
    }
    if (method === 'config.set' && params.key === 'model' && refuseModelWrite) {
      throw new Error('refused')
    }
    if (method === 'turn.send' && holdTurnSend) {
      return new Promise<Record<string, unknown>>((done) => { releaseSend = () => done({}) })
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
    send: runtime.send,
    /* Land the refusal's reload, and answer a held `turn.send`. */
    landReloads: () => { reloads.splice(0).forEach((done) => done()) },
    releaseSend: () => releaseSend(),
    isDraft: () => runtime.isDraft(),
    viewGen: () => generation(),
    enterDraft,
    stage,
    onCreate: (fn: () => void) => { bumpOnCreate = fn },
    /* Counted on the transport rather than in the log, which a new draft
       clears. */
    minted: () => minted,
    reReads,
    options,
    answerHeld: (model: string) => { options.held.splice(0).forEach((r) => r.resolve({ model, provider: 'p', providers: [] })) },
    failHeld: () => { options.held.splice(0).forEach((r) => r.reject(new Error('socket disconnected'))) },
    rereadChips: registry.rereadChips,
    log,
    rows,
    pointer: () => current,
    created,
    workdir,
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

  it('hands the folder the draft picked to the create, and to the row', async () => {
    /* The create is the only moment the engine takes a working directory, so
       the pick rides it; the row carries it too, so the rail groups the new
       conversation before the list is read back; and the pick is spent, so the
       next draft starts on the default. */
    const h = await harness(true)
    h.workdir.pick('/w/thesis')
    await h.openConversation()
    expect(h.created).toEqual([{ workdir: '/w/thesis' }])
    expect(h.rows[0]!.workdir).toBe('/w/thesis')
    expect(h.workdir.staged()).toBeNull()
  })

  it('creates on the default, and says nothing about a folder, when none was picked', async () => {
    const h = await harness(true)
    await h.openConversation()
    expect(h.created).toEqual([{}])
    expect(h.rows[0]!.workdir).toBeNull()
  })

  it('drops a picked folder with the draft it was picked on', async () => {
    const h = await harness(true)
    h.workdir.pick('/w/thesis')
    h.enterDraft()
    expect(h.workdir.staged()).toBeNull()
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

/* The first message of a draft records the model its turn runs on in the
   picker's recent list (features/model/recent.ts). That is the staged pick when
   the server applies it to the conversation just made, and the model the session
   already had when it refuses -- which the chip shows only once its reload
   lands, so that turn records nothing rather than the pick it did not run on. */
describe('the model the first message of a draft goes out on', () => {
  const used = (): unknown => JSON.parse(localStorage.getItem('raven.models.used') || 'null')
  const settle = async (): Promise<void> => { for (let i = 0; i < 20; i += 1) await Promise.resolve() }

  it('records the staged model when the server applies it', async () => {
    localStorage.clear()
    const h = await harness(true)
    const model = await import('../../features/model/store')
    h.stage({ model: { model: 'staged-m', provider: 'p' } })
    model.setCurrent('staged-m', 'p')

    h.send('hello')
    await settle()

    expect(h.log).toContain('rpc:turn.send')
    expect(used()).toEqual([{ model: 'staged-m', provider: 'p' }])
  })

  it('records the model the chip named as the message left, not one picked while it was on its way', async () => {
    localStorage.clear()
    const h = await harness(true, { holdTurnSend: true })
    const model = await import('../../features/model/store')
    h.stage({ model: { model: 'staged-m', provider: 'p' } })
    model.setCurrent('staged-m', 'p')

    h.send('hello')
    await settle()
    expect(h.log).toContain('rpc:turn.send')
    model.setCurrent('other-m', 'p')
    h.releaseSend()
    await settle()

    expect(used()).toEqual([{ model: 'staged-m', provider: 'p' }])
  })

  it('counts again once a refused stage\'s reload has landed, on a conversation the roster made', async () => {
    /* The roster's new-instance button promotes a draft with no send to read the
       refusal. Once the reload lands the chip shows what the session runs, and
       the first send, whenever it comes, records it. */
    localStorage.clear()
    const h = await harness(true, { refuseModelWrite: true })
    const model = await import('../../features/model/store')
    h.stage({ model: { model: 'staged-m', provider: 'p' } })
    await h.openConversation()
    h.landReloads()
    await settle()
    /* What the landed reload puts on the chip; the fake does not paint it. */
    model.setCurrent('real-m', 'p')

    h.sendOnSession('hello', () => {})
    await settle()

    expect(used()).toEqual([{ model: 'real-m', provider: 'p' }])
  })

  it('records nothing when the server refuses the staged model', async () => {
    localStorage.clear()
    const h = await harness(true, { refuseModelWrite: true })
    const model = await import('../../features/model/store')
    h.stage({ model: { model: 'staged-m', provider: 'p' } })
    model.setCurrent('staged-m', 'p')

    h.send('hello')
    await settle()

    expect(h.log).toContain('rpc:turn.send')
    expect(used()).toBeNull()
  })
})

/* The draft and the conversation it becomes are one view: the promotion takes no
   new view ticket, and the selection the draft read is what the session starts
   on. So the first re-read of that view -- a settings-changing tool result, a
   provider or default-model write, a typed id -- is a refresh of a known
   selection and must leave it usable, while every way of reaching a DIFFERENT
   view still waits for that view's own answer. Driven on the real chip source
   and store, with only the transport faked, on gateways with and without the
   selection-only read. */
describe('the model chip across a promotion', () => {
  const settle = async (): Promise<void> => { for (let i = 0; i < 20; i += 1) await Promise.resolve() }
  const chip = async () => {
    const store = await import('../../features/model/store')
    const source = await import('../../features/model/source')
    return { status: store.loadStatus(), current: store.current(), blocked: source.blockSendForModel() }
  }

  it.each([true, false])('keeps a known selection usable through the first re-read of a promoted draft (selection-only: %s)', async (selectionOnly) => {
    const h = await harness(true, { realModel: true, selectionOnly })
    await settle()
    expect(await chip()).toEqual({ status: 'ready', current: 'draft-m', blocked: false })
    await h.openConversation()
    expect(h.pointer()).toBe('made-1')

    h.options.holdOptions = true
    h.rereadChips()
    await settle()
    expect(h.options.held.length).toBeGreaterThan(0)
    expect(await chip()).toEqual({ status: 'ready', current: 'draft-m', blocked: false })

    h.answerHeld('session-m')
    await settle()
    expect(await chip()).toEqual({ status: 'ready', current: 'session-m', blocked: false })
  })

  it.each([true, false])('still waits when the draft never learned its selection (selection-only: %s)', async (selectionOnly) => {
    const h = await harness(false, { realModel: true, selectionOnly })
    h.options.holdOptions = true
    h.enterDraft()
    await settle()
    h.failHeld()
    await settle()
    expect((await chip()).status).toBe('error')
    await h.openConversation()

    h.rereadChips()
    await settle()
    expect(await chip()).toMatchObject({ status: 'loading', blocked: true })
    h.answerHeld('session-m')
    await settle()
    expect(await chip()).toEqual({ status: 'ready', current: 'session-m', blocked: false })
  })

  it.each([true, false])('waits again for a new draft opened after the promotion (selection-only: %s)', async (selectionOnly) => {
    const h = await harness(true, { realModel: true, selectionOnly })
    await settle()
    await h.openConversation()
    h.options.holdOptions = true
    h.enterDraft()
    await settle()
    expect(await chip()).toMatchObject({ status: 'loading', blocked: true })
    h.answerHeld('default-m')
    await settle()
    expect(await chip()).toEqual({ status: 'ready', current: 'default-m', blocked: false })
  })

  it.each([true, false])('hands nothing to the conversation when the reader left during its creation (selection-only: %s)', async (selectionOnly) => {
    const h = await harness(true, { realModel: true, selectionOnly })
    await settle()
    /* The reader opens a new draft while `session.create` is answering; that
       draft's own read answers too, so the page holds a ready selection that
       belongs to the NEW draft, not to the conversation being made. */
    h.onCreate(() => { h.enterDraft() })
    await h.openConversation()
    await settle()
    expect((await chip()).status).toBe('ready')

    h.options.holdOptions = true
    h.rereadChips()
    await settle()
    expect(h.pointer()).toBe('made-1')
    expect(await chip()).toMatchObject({ status: 'loading', blocked: true })
  })
})
