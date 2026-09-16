// @vitest-environment happy-dom
/* Turning the draft on screen into a real conversation.
 *
 * Two callers depend on this and only one of them is a send: the composer's
 * first message, and the sub-agent roster's new-instance button, which needs a
 * conversation for the instance to live in. The live layer is plain script, so
 * the function is sliced out and driven directly -- the same way
 * `features/rail/session-delete.test.ts` pins its neighbour in this file.
 *
 * What is pinned here is the ORDER, which is the part a reader cannot see and
 * the send silently depends on: the pointer moves, then the caller's hook runs,
 * then the staged settings go up. The hook is where `liveSend` records the
 * turn's owner, and a reader switching conversations inside those round trips
 * would otherwise leave the in-flight turn parked under the wrong one.
 */

// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'

import { describe, expect, it, vi } from 'vitest'

const source = readFileSync('src/live/080-overrides.js', 'utf8') as string

/* Just the one function, not the file: the rest of this layer reaches for
   dozens of globals that have nothing to do with the promotion. */
/* Both halves: the entry point that decides whether to promote at all, and the
   promotion itself. They are one unit of behaviour and the first calls the
   second. */
const slice = (start: string): string => {
  const begin = source.indexOf(start)
  if (begin < 0) throw new Error(`open-conversation.test: ${start} is gone from 080-overrides.js`)
  return source.slice(begin, source.indexOf('\n}\n', begin) + 2)
}
const fnSource = `let promoting = null;\n`
  + `${slice('async function openConversation(preview, atPointer) {')}\n`
  + `${slice('async function promote(preview, atPointer) {')}\n`
  + `${slice('function sendOnSession(text, failed) {')}\n`
  + slice('function dispatchSend(text, failed) {')

interface Row { id: string; title: string; last: string; persisted: boolean }

function harness(startAsDraft: boolean) {
  const log: string[] = []
  const rows: Row[] = []
  let pointer: string | null = startAsDraft ? null : 'already-open'
  let minted = 0
  const rpc = {
    call: vi.fn(async (method: string) => {
      log.push(`rpc:${method}`)
      minted += 1
      return { session_id: `made-${minted}`, info: { cwd: '/w' } }
    }),
  }
  const say = (name: string) => (...args: unknown[]): void => {
    log.push(args.length && typeof args[0] === 'string' ? `${name}:${args[0] as string}` : name)
  }
  const build = new Function(
    'rpc', 'T', 'sessionCurrent', 'sessionSet', 'sessionRows', 'claimDraft',
    'applyStagedModel', 'applyStagedTier', 'applyStagedPerm', 'sessionDraw',
    'subscribe', 'wsSetRoot', 'startAsDraft', 'touchSession', 'beginNaming',
    'mediaOf', 'namingDeclined', 'setSessionWorkdir',
    `let draft = startAsDraft; let viewGen = 7; const park = { turnOwner: null, lastAsk: '' }; let pendingWorkdir = null;\n${fnSource}\n`
    + 'return { openConversation, sendOnSession, isDraft: () => draft, '
    + 'turnOwner: () => park.turnOwner, setDraft: (on) => { draft = on; }, '
    + 'stageWorkdir: (dir) => { pendingWorkdir = dir; }, stagedWorkdir: () => pendingWorkdir };',
  ) as (...args: unknown[]) => {
    openConversation: (preview?: string, atPointer?: (id: string) => void) => Promise<string | null>
    sendOnSession: (text: string, failed: (e: unknown) => void) => void
    isDraft: () => boolean
    turnOwner: () => string | null
    setDraft: (on: boolean) => void
    stageWorkdir: (dir: string | null) => void
    stagedWorkdir: () => string | null
  }
  const built = build(
    rpc,
    (key: string) => key,
    () => pointer,
    (id: string | null) => { pointer = id; log.push(`pointer:${String(id)}`) },
    () => rows,
    say('claimDraft'),
    async (...a: unknown[]) => { log.push(`staged:model:${String(a[1])}`) },
    say('staged:tier'),
    say('staged:perm'),
    say('draw'),
    async (id: string) => { log.push(`subscribe:${id}`) },
    say('wsRoot'),
    startAsDraft,
    (id: string) => { log.push(`touch:${id}`) },
    () => { log.push('naming') },
    () => ({}),
    say('namingDeclined'),
    say('workdir'),
  )
  return { ...built, log, rows, rpc, pointer: () => pointer }
}

describe('getting a conversation to work in', () => {
  it('answers the open one, and makes nothing, when there already is one', async () => {
    /* "Give me a conversation" is what both callers want, so the seam answers it
       rather than having to be asked separately whether it applies. */
    const h = harness(false)
    await expect(h.openConversation()).resolves.toBe('already-open')
    expect(h.rpc.call).not.toHaveBeenCalled()
    expect(h.log).toEqual([])
    expect(h.rows).toEqual([])
  })

  it('hands the staged folder to the create, puts it on the row, and spends it', async () => {
    const h = harness(true)
    h.stageWorkdir('/w/thesis')
    await expect(h.openConversation()).resolves.toBe('made-1')
    expect(h.rpc.call).toHaveBeenCalledWith('session.create', { workdir: '/w/thesis' })
    expect((h.rows[0] as Row & { workdir?: string | null }).workdir).toBe('/w/thesis')
    /* Spent by the create, so the next draft starts from no folder; and the
       chip is told the conversation it now reports on. */
    expect(h.stagedWorkdir()).toBeNull()
    expect(h.log).toContain('workdir:/w/thesis')
  })

  it('creates with no folder when none was staged, and the chip is told so', async () => {
    const h = harness(true)
    await h.openConversation()
    expect(h.rpc.call).toHaveBeenCalledWith('session.create', {})
    expect((h.rows[0] as Row & { workdir?: string | null }).workdir).toBeNull()
    expect(h.log).toContain('workdir')
  })

  it('promotes the draft, and answers with the conversation it made', async () => {
    const h = harness(true)
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
    const h = harness(true)
    await h.openConversation('do the thing')
    expect(h.rows[0]!.last).toBe('do the thing')
  })

  it('runs the caller hook once the pointer has moved and before the settings go up', async () => {
    const h = harness(true)
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
      'workdir',
      'staged:model:7',
      'staged:tier:made-1',
      'staged:perm:made-1',
      'draw',
      'subscribe:made-1',
    ])
  })

  it('mints one conversation for two callers that land inside the same promotion', async () => {
    /* `draft` stays raised across `session.create`, so a second press arriving in
       that window used to read the page as still a draft. Both callers want the
       same conversation; both get it, and each one's hook still runs. */
    const h = harness(true)
    const hooks: string[] = []
    const [a, b] = await Promise.all([
      h.openConversation('first', (id) => hooks.push(`a:${id}`)),
      h.openConversation('second', (id) => hooks.push(`b:${id}`)),
    ])
    expect([a, b]).toEqual(['made-1', 'made-1'])
    expect(h.rpc.call).toHaveBeenCalledTimes(1)
    expect(h.rows).toHaveLength(1)
    expect(hooks.sort()).toEqual(['a:made-1', 'b:made-1'])
  })

  it('promotes the NEXT draft too, rather than answering with the last one', async () => {
    /* The hold is released when the promotion ends, not kept for the life of the
       page: the reader goes back to the new-task screen -- `startDraft` raises
       the flag again -- and that draft has to become its own conversation. A
       hold left standing would hand it the previous one, and the instance would
       be filed under a conversation the reader had left. */
    const h = harness(true)
    await expect(h.openConversation()).resolves.toBe('made-1')
    h.setDraft(true)
    await expect(h.openConversation()).resolves.toBe('made-2')
    expect(h.rpc.call).toHaveBeenCalledTimes(2)
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
    const h = harness(true)
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
    for (const step of ['staged:model:7', 'staged:tier:made-1', 'staged:perm:made-1', 'subscribe:made-1']) {
      expect(h.log.indexOf(step)).toBeGreaterThan(-1)
      expect(sent).toBeGreaterThan(h.log.indexOf(step))
    }
  })

  it('carries the view generation the promotion began on into the staged model', async () => {
    /* Taken before the first await, so a late answer is checked against the view
       as it stood when this started rather than whatever is open by then. */
    const h = harness(true)
    await h.openConversation()
    expect(h.log).toContain('staged:model:7')
  })
})
