/** Tests for the session's delivery registry and how it survives a park. */

// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'

import { setCurrent } from '../../lib/session'
import { resetSources, setSources } from '../../state/sources'
import * as deliveries from './deliveries'
import * as workspace from './store'

import type { WorkspaceSource } from './types'

const manifest = (files: Array<Record<string, unknown>>): unknown => ({ raven_delivery: { files } })

afterEach(() => {
  deliveries.restore([])
  vi.unstubAllGlobals()
})

describe('delivery registry', () => {
  it('reads a manifest and lists the newest turn first, manifest order within it', () => {
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: '/w/a.md', name: 'a.md', title: 'A', size: 12 }]))
    deliveries.record(deliveries.SESSION, 2, manifest([
      { path: '/w/b.csv', name: 'b.csv', size: 3 },
      { path: '/w/c.pdf', name: 'c.pdf', size: 4 },
    ]))

    expect(deliveries.list().map((row) => row.name)).toEqual(['b.csv', 'c.pdf', 'a.md'])
    expect(deliveries.ofTurn(deliveries.SESSION, 1).map((row) => row.title)).toEqual(['A'])
    /* No title given: the file's own name is the one thing always there. */
    expect(deliveries.ofTurn(deliveries.SESSION, 2)[0]?.title).toBe('b.csv')
    expect(deliveries.ofTurn(deliveries.SESSION, 2)[0]?.ext).toBe('csv')
  })

  /* The registry keys the gateway's resolved path; a caller may hold the path
     with the platform's other separator or a redundant segment. The same file
     is the same row -- but a bare name is not enough to match on, because two
     delivered files can share one. */
  it('finds a row under either separator and a redundant segment, but not by name', () => {
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: 'D:\\work\\out\\a.md', name: 'a.md' }]))

    expect(deliveries.byPath('d:/work/out/a.md')?.name).toBe('a.md')
    expect(deliveries.byPath('D:\\work\\out\\.\\a.md')?.name).toBe('a.md')
    expect(deliveries.byPath('a.md')).toBeNull()
  })

  /* A UNC path is case-insensitive on Windows even though it carries no drive
     letter; the registry may hold the gateway's spelling and the panel
     another. */
  it('finds a UNC row under a differently-cased spelling, and marks it there', () => {
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: '\\\\server\\share\\out\\a.md', name: 'a.md' }]))

    expect(deliveries.byPath('\\\\SERVER\\share\\out\\a.md')?.name).toBe('a.md')
    deliveries.markMissing('\\\\SERVER\\share\\out\\a.md')
    expect(deliveries.byPath('\\\\server\\share\\out\\a.md')?.missing).toBe(true)
  })

  /* A UNC path is a file on the share and an absolute path under the local
     root is a file here, even where the two spell the same: a registry that
     answered for both would hand one file's download token to the other. */
  it('keeps a UNC path apart from an absolute path that spells the same', () => {
    deliveries.record(deliveries.SESSION, 1, manifest([
      { path: '\\\\server\\share\\out\\a.md', name: 'a.md', download_path: '/t/unc' },
    ]))
    deliveries.record(deliveries.SESSION, 2, manifest([
      { path: '/server/share/out/a.md', name: 'a.md', download_path: '/t/local' },
    ]))

    expect(deliveries.byPath('\\\\server\\share\\out\\a.md')?.downloadPath).toBe('/t/unc')
    expect(deliveries.byPath('/server/share/out/a.md')?.downloadPath).toBe('/t/local')
  })

  it('does not answer a UNC spelling with an absolute path of the same name', () => {
    deliveries.record(deliveries.SESSION, 1, manifest([
      { path: '/server/share/out/a.md', name: 'a.md', download_path: '/t/local' },
    ]))

    expect(deliveries.byPath('\\\\server\\share\\out\\a.md')).toBeNull()
  })

  /* A Windows directory can be case-sensitive, and then two delivered files
     whose paths differ only by case are different files. The exact spelling
     wins; folding is only the fallback when no row matches exactly. */
  it('keeps case-differing files apart when the exact spelling has a row', () => {
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: 'C:\\work\\A.md', name: 'A.md', download_path: '/t/upper' }]))
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: 'C:\\work\\a.md', name: 'a.md', download_path: '/t/lower' }]))

    expect(deliveries.byPath('C:\\work\\A.md')?.downloadPath).toBe('/t/upper')
    expect(deliveries.byPath('C:\\work\\a.md')?.downloadPath).toBe('/t/lower')
    /* The drive letter itself is never case-significant. */
    expect(deliveries.byPath('c:\\work\\a.md')?.downloadPath).toBe('/t/lower')

    deliveries.markMissing('C:\\work\\A.md')
    expect(deliveries.byPath('C:\\work\\A.md')?.missing).toBe(true)
    expect(deliveries.byPath('C:\\work\\a.md')?.missing).toBe(false)
  })

  /* A doubled separator after the drive letter is still that drive's path, and
     a drive letter is not a UNC server: the two slashes after it are one
     separator too many, not the start of a share. */
  it('keeps the drive letter when the path carries a doubled separator', () => {
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: 'C:\\work\\out\\a.md', name: 'a.md' }]))

    expect(deliveries.byPath('C://work/out/a.md')?.name).toBe('a.md')
  })

  /* The redirector resolves a UNC server and share case-insensitively, so
     that much of the path folds with the spelling: it is one file, and a
     write under the other spelling is a write on the same row. Below the
     share the volume decides, and the page cannot: that spelling is another
     file until the server says otherwise. */
  it('folds the case of a UNC server and share, and of nothing below them', () => {
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: '\\\\server\\share\\out\\a.md', name: 'a.md' }]))

    expect(deliveries.exactMatch('\\\\SERVER\\share\\out\\a.md')).toBe(true)
    expect(deliveries.exactMatch('\\\\server\\share\\OUT\\a.md')).toBe(false)
  })

  /* The write side may not cross to a neighbour: the panel's path can name a
     file that is not there at all, and greying the row that IS there would
     take a live delivery off the shelf and strip its download link. */
  it('does not mark a case-variant neighbour missing', () => {
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: 'C:\\work\\A.md', name: 'A.md' }]))

    deliveries.markMissing('C:\\work\\a.md')

    expect(deliveries.byPath('C:\\work\\A.md')?.missing).toBe(false)
  })

  /* Only the server can say which of two spellings is the file: it resolves
     the path on the gateway's own volume. So a row the panel's path names
     only by folding is asked about by its own path, and it is greyed only
     when that file is gone as well. */
  it('keeps a live delivery when the panel named a case-variant file that is gone', async () => {
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: 'C:\\work\\A.md', name: 'A.md' }]))
    const asked: string[] = []
    vi.stubGlobal('fetch', (url: string) => {
      asked.push(String(url))
      /* The panel's spelling is not there; the registry's own spelling is. */
      return Promise.resolve({ status: String(url).includes('a.md') ? 404 : 200 })
    })

    await workspace.probeDeliveryMissing('C:\\work\\a.md')

    expect(deliveries.byPath('C:\\work\\A.md')?.missing).toBe(false)
    /* And it asked about the row's own path, not the panel's. */
    expect(asked.some((url) => url.includes('A.md'))).toBe(true)
  })

  /* A path that names no delivery has no row to mark, and nothing to ask the
     server about either. */
  it('does nothing when the path names no delivery', async () => {
    const asked: string[] = []
    vi.stubGlobal('fetch', (url: string) => {
      asked.push(String(url))
      return Promise.resolve({ status: 404 })
    })

    await workspace.markDeliveryGone('/w/nothing.md')

    expect(asked).toEqual([])
    expect(deliveries.list()).toEqual([])
  })

  /* The other way round, and the ordinary case: the registry holds the path
     the gateway resolved and the panel holds the one the model typed, the
     file is gone, and the row has to go grey. */
  it('greys a row whose own file is gone when the panel named a case-variant', async () => {
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: 'C:\\work\\a.md', name: 'a.md' }]))
    vi.stubGlobal('fetch', () => Promise.resolve({ status: 404 }))

    await workspace.probeDeliveryMissing('C:\\work\\A.md')

    expect(deliveries.byPath('C:\\work\\a.md')?.missing).toBe(true)
  })

  /* POSIX paths are case-sensitive, so they must never take the folded road. */
  it('does not fold the case of a POSIX path', () => {
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: '/w/A.md', name: 'A.md' }]))

    expect(deliveries.byPath('/w/a.md')).toBeNull()
  })

  it('shows a re-delivered file once, at the turn that delivered it again', () => {
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: '/w/a.md', name: 'a.md', size: 12 }]))
    deliveries.record(deliveries.SESSION, 3, manifest([{ path: '/w/a.md', name: 'a.md', size: 40 }]))

    expect(deliveries.count()).toBe(1)
    expect(deliveries.list().map((row) => row.turn)).toEqual([3])
    expect(deliveries.byPath('/w/a.md')?.size).toBe(40)
    expect(deliveries.byPath('/w/a.md')?.turn).toBe(3)
  })

  /* The shelf collapses a path to its newest delivery; the turn that delivered
     the earlier copy still has to be able to say what IT delivered, because
     that is what its own card in the transcript renders -- and on a reload the
     whole history replays before anything is drawn. */
  it('leaves an earlier turn holding what that turn delivered', () => {
    deliveries.record(deliveries.SESSION, 2, manifest([{ path: '/w/r.md', name: 'r.md', title: 'v1' }]))
    deliveries.record(deliveries.SESSION, 5, manifest([{ path: '/w/r.md', name: 'r.md', title: 'v2' }]))

    expect(deliveries.ofTurn(deliveries.SESSION, 2).map((row) => row.title)).toEqual(['v1'])
    expect(deliveries.ofTurn(deliveries.SESSION, 5).map((row) => row.title)).toEqual(['v2'])
    expect(deliveries.list().map((row) => row.title)).toEqual(['v2'])
  })

  it('re-delivering within one turn refreshes that turn\'s row rather than doubling it', () => {
    deliveries.record(deliveries.SESSION, 4, manifest([{ path: '/w/r.md', name: 'r.md', size: 10 }]))
    deliveries.record(deliveries.SESSION, 4, manifest([{ path: '/w/r.md', name: 'r.md', size: 99 }]))

    expect(deliveries.ofTurn(deliveries.SESSION, 4).map((row) => row.size)).toEqual([99])
  })

  it('takes the runtime word that a replayed file is gone, and the viewer\'s later', () => {
    deliveries.record(deliveries.SESSION, 1, manifest([
      { path: '/w/a.md', name: 'a.md', missing: true },
      { path: '/w/b.md', name: 'b.md' },
    ]))
    expect(deliveries.byPath('/w/a.md')?.missing).toBe(true)
    expect(deliveries.byPath('/w/b.md')?.missing).toBe(false)

    deliveries.markMissing('/w/b.md')
    expect(deliveries.byPath('/w/b.md')?.missing).toBe(true)
  })

  /* One file, one answer: the turn cards must not still show it as present
     while the shelf shows it gone. */
  it('marks every turn that delivered the file, not just the newest', () => {
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: '/w/r.md', name: 'r.md' }]))
    deliveries.record(deliveries.SESSION, 4, manifest([{ path: '/w/r.md', name: 'r.md' }]))

    deliveries.markMissing('/w/r.md')

    expect(deliveries.ofTurn(deliveries.SESSION, 1)[0]?.missing).toBe(true)
    expect(deliveries.ofTurn(deliveries.SESSION, 4)[0]?.missing).toBe(true)
  })

  it('notifies a subscriber when a delivery lands, and not for an empty manifest', () => {
    let seen = 0
    const stop = deliveries.subscribe(() => { seen += 1 })
    deliveries.record(deliveries.SESSION, 1, manifest([]))
    deliveries.record(deliveries.SESSION, 1, { raven_delivery: { files: [{ name: 'no path' }] } })
    expect(seen).toBe(0)
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: '/w/a.md', name: 'a.md' }]))
    expect(seen).toBe(1)
    stop()
    deliveries.record(deliveries.SESSION, 2, manifest([{ path: '/w/b.md', name: 'b.md' }]))
    expect(seen).toBe(1)
  })

  /* The registry is what answers after a reconnect: the transcript comes back
     from disk, and a turn still in flight was never written to it. */
  it('fills the shelf from the gateway registry, newest first', () => {
    deliveries.seed([
      { path: '/w/old.md', name: 'old.md', title: 'Older', size: 10, created_at: '2026-08-01T00:00:00Z' },
      { path: '/w/new.md', name: 'new.md', title: 'Newer', size: 20, created_at: '2026-08-02T00:00:00Z' },
    ])

    expect(deliveries.list().map((row) => row.title)).toEqual(['Newer', 'Older'])
    /* No turn to name: the registry knows the conversation delivered it, not
       where in the reading that was. */
    expect(deliveries.list()[0]?.turn).toBeNull()
    expect(deliveries.count()).toBe(2)
  })

  it('lets a turn keep the path the registry also has, and adds only the rest', () => {
    deliveries.record(deliveries.SESSION, 4, manifest([{ path: '/w/a.md', name: 'a.md', title: 'from the turn' }]))
    deliveries.seed([
      { path: '/w/a.md', name: 'a.md', title: 'from the registry' },
      { path: '/w/b.md', name: 'b.md', title: 'only in the registry' },
    ])

    expect(deliveries.list().map((row) => row.title)).toEqual(['from the turn', 'only in the registry'])
    expect(deliveries.ofTurn(deliveries.SESSION, 4).map((row) => row.title)).toEqual(['from the turn'])
    expect(deliveries.count()).toBe(2)
  })

  it('seeds the same registry twice without doubling it', () => {
    const files = [{ path: '/w/a.md', name: 'a.md' }]
    deliveries.seed(files)
    deliveries.seed(files)

    expect(deliveries.count()).toBe(1)
    expect(deliveries.list()).toHaveLength(1)
  })

  it('ignores an answer that is not a list', () => {
    deliveries.seed(null)
    deliveries.seed({ files: [] })
    expect(deliveries.list()).toEqual([])
  })

  /* The one way these rows can vanish without anybody noticing: a conversation
     switched away from is restored from the parked snapshot, never from a
     replay, so a snapshot that does not carry them loses the live turn's. */
  it('rides the workspace snapshot through a park and back', () => {
    deliveries.record(deliveries.SESSION, 2, manifest([{ path: '/w/a.md', name: 'a.md', title: 'A' }]))
    const parked = workspace.snapshot()

    workspace.restore({ changes: [], urls: [], file: null, turn: 0, unseen: 0, deliveries: [] })
    expect(deliveries.count()).toBe(0)

    workspace.restore(parked)
    expect(deliveries.list().map((row) => row.title)).toEqual(['A'])
  })

  it('asks the source for the conversation, once, and takes what it answers', async () => {
    const asked: string[] = []
    setSources({
      workspace: {
        shortPath: (p: string) => p,
        hostPlatform: () => 'mac',
        deliverables: async (key: string) => {
          asked.push(key)
          return [{ path: '/w/a.md', name: 'a.md', title: 'recovered' }]
        },
      } as WorkspaceSource,
    })

    setCurrent('tui:s1')
    await workspace.loadDeliveries('tui:s1')

    expect(asked).toEqual(['tui:s1'])
    expect(deliveries.list().map((row) => row.title)).toEqual(['recovered'])
    setCurrent(null)
    resetSources()
  })

  /* The reader can click another conversation while the answer is in flight,
     and the registry it would land in is that one's. */
  it('drops an answer for a conversation the reader has already left', async () => {
    let release: (rows: unknown) => void = () => {}
    setCurrent('tui:a')
    setSources({
      workspace: {
        shortPath: (p: string) => p,
        hostPlatform: () => 'mac',
        deliverables: () => new Promise((resolve) => { release = resolve }),
      } as WorkspaceSource,
    })

    const pending = workspace.loadDeliveries('tui:a')
    /* The switch: B's own desk, and B's own registry. */
    setCurrent('tui:b')
    release([{ path: '/a/secret.md', name: 'secret.md', title: "A's file" }])
    await pending

    expect(deliveries.list()).toEqual([])
    setCurrent(null)
    resetSources()
  })

  /* A source without the verb is an older gateway; a source that throws is one
     that is there and unhappy. Neither may cost the shelf what it already has. */
  it('keeps what the turns gave it when the registry cannot be read', async () => {
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: '/w/a.md', name: 'a.md', title: 'from the turn' }]))
    setSources({
      workspace: {
        shortPath: (p: string) => p,
        hostPlatform: () => 'mac',
        deliverables: async () => { throw new Error('nope') },
      } as WorkspaceSource,
    })

    await workspace.loadDeliveries('tui:s1')
    setSources({ workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac' } as WorkspaceSource })
    await workspace.loadDeliveries('tui:s1')

    expect(deliveries.list().map((row) => row.title)).toEqual(['from the turn'])
    resetSources()
  })

  it('is emptied by a workspace reset, which is what a session switch does', () => {
    deliveries.record(deliveries.SESSION, 1, manifest([{ path: '/w/a.md', name: 'a.md' }]))
    workspace.reset()
    expect(deliveries.list()).toEqual([])
  })

  it('does not read one stream\'s turn number against another\'s', () => {
    /* Turn numbers are per stream: a delegated run counts from one exactly as
       the conversation does. The session-wide readers rank by that number as if
       it were one clock, so an agent's tenth turn outranks the conversation's
       second -- the shelf puts the older row on top, and a path both of them
       delivered resolves to the older one's title and token. */
    const file = (name: string, extra: Record<string, unknown> = {}): Record<string, unknown> => ({
      raven_delivery: { files: [{ path: `/w/${name}`, name, title: name, size: 1, download_path: `/t/${name}`, ...extra }], invalid: [] },
    })
    deliveries.record('agent:a1', 10, file('shared.md', { title: 'from the sub-agent' }))
    deliveries.record(deliveries.SESSION, 2, file('shared.md', { title: 'from the conversation' }))
    deliveries.record(deliveries.SESSION, 2, file('later.md'))

    /* The conversation delivered it last, so that is what the file is now. */
    expect(deliveries.byPath('/w/shared.md')?.title).toBe('from the conversation')
    /* And the shelf reads newest first, by when the rows arrived. */
    expect(deliveries.list().map((r) => r.name)).toEqual(['later.md', 'shared.md'])
  })

  it('does not move a delivery to the front for being recorded twice', () => {
    /* Live records the manifest off the tool event; a reload records the same
       manifest again off the replayed history, and a pane that stays open
       re-reads the same turn. None of that is the file being handed over a
       second time, so none of it may move the row up the shelf. */
    const file = (name: string): Record<string, unknown> => ({
      raven_delivery: { files: [{ path: `/w/${name}`, name, title: name, size: 1, download_path: `/t/${name}` }], invalid: [] },
    })
    deliveries.record(deliveries.SESSION, 1, file('early.md'))
    deliveries.record(deliveries.SESSION, 2, file('late.md'))
    expect(deliveries.list().map((r) => r.name)).toEqual(['late.md', 'early.md'])

    deliveries.record(deliveries.SESSION, 1, file('early.md'))
    expect(deliveries.list().map((r) => r.name)).toEqual(['late.md', 'early.md'])
  })
})

describe('chronology across streams', () => {
  /* Paint order is not delivery order, and neither is the event source. A
     delegated history loads lazily, so opening an older run AFTER a newer
     delivery paints the older row second; a background sub-agent can deliver
     later than a live main-lane call and still paint afterwards. The manifest
     carries the moment the gateway wrote it, and that is what decides. */
  const at = (when: number | null, over: Record<string, unknown> = {}) => ({
    raven_delivery: {
      ...(when == null ? {} : { delivered_at: when }),
      files: [{ path: '/w/report.md', name: 'report.md', title: 'newer', size: 200, ...over }],
    },
  })
  const older = { title: 'older', size: 10 }

  it('keeps the newer delivery in front when an older stream is opened after it', () => {
    deliveries.record(deliveries.SESSION, 3, at(2_000), 'c-new')
    deliveries.record('agent:one', 1, at(1_000, older), 'c-old')

    expect(deliveries.byPath('/w/report.md')?.title).toBe('newer')
    expect(deliveries.list()[0]?.title).toBe('newer')
  })

  it('still puts a genuinely later delivery in front', () => {
    deliveries.record('agent:one', 1, at(1_000, older), 'c-old')
    deliveries.record(deliveries.SESSION, 3, at(2_000), 'c-new')

    expect(deliveries.byPath('/w/report.md')?.title).toBe('newer')
    expect(deliveries.list()[0]?.title).toBe('newer')
  })

  it('lets a background run that delivered later win over an earlier live call', () => {
    /* The case a source-based rule cannot answer: the live main-lane delivery
       arrives first, the sub-agent keeps running, and its newer delivery is
       painted from history afterwards. Both carry the gateway's own stamp, so
       neither has to be guessed at. */
    deliveries.record(deliveries.SESSION, 3, at(1_000, older), 'c-live')
    deliveries.record('agent:one', 1, at(2_000), 'c-bg')

    expect(deliveries.byPath('/w/report.md')?.title).toBe('newer')
    expect(deliveries.list()[0]?.title).toBe('newer')
  })

  it('answers the same however three rows arrive', () => {
    /* A total order does not depend on the order it is asked about. */
    for (const order of [[2_000, 3_000, 1_000], [1_000, 3_000, 2_000], [3_000, 1_000, 2_000]]) {
      deliveries.restore([])
      order.forEach((when, i) => {
        deliveries.record(i === 1 ? deliveries.SESSION : `agent:${i}`, i + 1,
          at(when, when === 3_000 ? {} : older), `c-${when}`)
      })
      expect(deliveries.byPath('/w/report.md')?.title).toBe('newer')
    }
  })

  it('answers the same at an equal stamp however the page painted them', () => {
    /* Two producers can land on one stamp -- a single producer's never repeat.
       There is no chronology left to read, so the answer must at least not depend
       on which was painted first, which is the thing that was wrong to begin
       with. */
    const forward: string[] = []
    const reverse: string[] = []
    for (const [order, out] of [[['c-a', 'c-b'], forward], [['c-b', 'c-a'], reverse]] as const) {
      deliveries.restore([])
      order.forEach((call, i) => {
        deliveries.record(i === 0 ? deliveries.SESSION : 'agent:one', 1,
          at(1_000, { title: call }), call)
      })
      out.push(String(deliveries.byPath('/w/report.md')?.title))
    }

    expect(forward).toEqual(reverse)
  })

  it('sorts a manifest with no stamp oldest, by the order it arrived', () => {
    /* Written before the gateway stamped its manifests. */
    deliveries.record('agent:one', 1, at(null, older), 'c-none')
    deliveries.record(deliveries.SESSION, 3, at(1_000), 'c-stamped')

    expect(deliveries.byPath('/w/report.md')?.title).toBe('newer')
  })

  it('keeps replay order between two unstamped manifests, whatever they are called', () => {
    /* A whole legacy history is unstamped, and a tool-call id carries no order --
       ranking two of them by it would pick an arbitrary row where replay order at
       least follows the transcript. The later-replayed one wins even though its
       call sorts first. */
    deliveries.record('agent:one', 1, at(null, older), 'z-old-call')
    deliveries.record(deliveries.SESSION, 3, at(null), 'a-new-call')

    expect(deliveries.byPath('/w/report.md')?.title).toBe('newer')
    expect(deliveries.list()[0]?.title).toBe('newer')
  })
  it('does not reorder two unstamped streams because one of them was replayed', () => {
    /* The conversation's rows are dropped and re-recorded whenever the main lane
       replays -- `store.ts` clears `SESSION` before rebuilding it. Arrival is the
       only key two unstamped rows have, so re-recording handed the conversation a
       fresh higher one while the sub-agent kept its old one: the same two
       deliveries answered differently before and after a replay, and the answer
       had nothing to do with when either happened. A legacy history is entirely
       unstamped, so this is the whole pre-`delivered_at` corpus. */
    deliveries.record(deliveries.SESSION, 3, at(null, older), 'a-session')
    deliveries.record('agent:one', 1, at(null), 'z-agent')
    const before = deliveries.byPath('/w/report.md')?.title

    deliveries.dropScope(deliveries.SESSION)
    deliveries.record(deliveries.SESSION, 3, at(null, older), 'a-session')

    expect(deliveries.byPath('/w/report.md')?.title).toBe(before)
  })
})
