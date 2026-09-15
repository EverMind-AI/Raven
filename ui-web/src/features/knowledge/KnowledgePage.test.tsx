// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { KnowledgeApp } from './KnowledgePage'
import * as store from './store'

import type { KbBase, KbDoc, KbSearch, KnowledgeSource } from './types'
import type { Shell } from '../../shell/bridge'

function base(over: Partial<KbBase> & { id: string }): KbBase {
  return {
    name: 'handbook',
    description: '',
    embedding_model: 'bge-m3',
    dimensions: 1024,
    created_at: '2026-08-24T00:00:00',
    updated_at: '2026-08-24T00:00:00',
    documents: 0,
    ...over,
  }
}

/* The island runs against the same two seams production wires: a fake shell on
   window.RavenShell (T returns its key, so tests assert catalogue keys rather
   than translations) and a fixture source on window.DS.knowledge. */
const pages: (string | null)[] = []
const opened: number[] = []

/* What the toast module actually does: append into the page's standing host.
   Asserting on the DOM keeps this test honest about where the text ends up. */
function toastHost(): HTMLElement {
  let host = document.getElementById('toasts')
  if (!host) {
    host = document.createElement('div')
    host.id = 'toasts'
    document.body.appendChild(host)
  }
  return host
}
const toasts = (): string[] =>
  Array.from(toastHost().querySelectorAll('.toast .t')).map((e) => e.textContent || '')
const confirms: string[] = []

function doc(over: Partial<KbDoc> & { id: string }): KbDoc {
  return {
    base_id: 'b1',
    source: 'onboarding.md',
    media_type: 'text/markdown',
    size: 12,
    status: 'pending',
    chunk_count: 0,
    error: '',
    created_at: '',
    updated_at: '',
    ...over,
  }
}

function source(over: Partial<KnowledgeSource> = {}): void {
  const fakeShell = {
    T: (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    menuAt: () => {},
    confirmAsk: (_t: unknown, body: unknown, _l: unknown, fn: () => void) => {
      confirms.push(String(body))
      fn()
    },
    showPage: (id: string | null) => pages.push(id),
    openSet: () => opened.push(1),
    closeDetail: () => {},
  } as unknown as Shell
  window.RavenShell = fakeShell
  window.DS = {
    ...(window.DS || {}),
    knowledge: {
      status: async () => ({ configured: true, model: 'bge-m3' }),
      bases: async () => [],
      create: async (name: string) => base({ id: 'made', name }),
      remove: async () => ({}),
      documents: async () => [],
      upload: async (_b: string, file: File) => doc({ id: 'd1', source: file.name }),
      index: async (id: string) => doc({ id, status: 'ready', chunk_count: 2 }),
      search: async () => ({ hits: [], search_ms: 0, embed_ms: 0 }),
      removeDoc: async () => {},
      ...over,
    },
  }
}

async function mount() {
  const view = render(<KnowledgeApp />)
  await act(async () => {
    await store.load()
  })
  return view
}

afterEach(() => {
  cleanup()
  store._resetForTests()
  pages.length = 0
  opened.length = 0
  toastHost().replaceChildren()
  confirms.length = 0
  delete (window as { DS?: unknown }).DS
})

/* Open one row's action menu. The four operations live behind it now: four
   buttons on every row is a wall across a table, and the design puts them
   where a reader goes looking for "what can I do with this file". */
async function openRowMenu(name: string): Promise<void> {
  /* The table is a grid, so every cell is a sibling and there is no row
     element to scope to: the name cell and the actions cell are matched by
     position instead. Asking the name cell's parent for `.kbops .dots` finds
     the first one in the whole table, which is the right answer only when
     there is one row. */
  const names = [...document.querySelectorAll('.kbtable .td.nm')]
  const at = names.findIndex((c) => c.textContent === name)
  const dots = [...document.querySelectorAll('.kbtable .kbops .dots')][at] as HTMLButtonElement
  await act(async () => {
    dots.click()
  })
}

describe('the knowledge page', () => {
  it('sends an unconfigured reader to the section that configures it', async () => {
    /* The status alone named a state and stopped. The endpoint is set in a
       settings section, and a reader told only its name has to go hunting.
       Asserted through `window.sTab`, which is what the settings store's
       `setTab` writes and its `open` reads back before lifting the dialog. */
    source({ status: async () => ({ configured: false, model: '' }) })
    /* `open()` refreshes from the settings source; a stub keeps the click from
       rejecting into an unhandled promise. */
    ;(window.DS as Record<string, unknown>).settings = { load: async () => ({}) }
    window.sTab = 'usage'
    await mount()
    expect(screen.getByText('gui.kb.unconfigured')).toBeTruthy()
    expect(screen.getByText('gui.kb.unconfigured_where')).toBeTruthy()
    await act(async () => {
      screen.getByText('gui.kb.unconfigured_go').click()
    })
    expect(window.sTab).toBe('memory')
    /* And the dialog is actually opened. `sTab` alone passes with the open
       call deleted, which is a button that aims a dialog nobody raises. */
    expect(opened.length).toBe(1)
  })

  /* The hits UI left with the search box, and came back as Recall Test. What
     survived the move is the store logic underneath -- the request token --
     so these assert what the store holds rather than what is painted, which
     is also where the race they pin actually lives. */
  it('keeps the newest answer when an older one lands after it', async () => {
    /* Both requests are for the same base, so the openId guard passes for each:
       without a request token the slower question repainted the panel under the
       answer to the newer one. */
    const gates: Array<(r: KbSearch) => void> = []
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd1', status: 'ready' })],
      search: () => new Promise((r) => gates.push(r as never)),
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    const first = store.searchNow('quarterly r')
    const second = store.searchNow('quarterly revenue')
    await act(async () => {
      /* The newest answers first, the stale one second -- the order that broke it. */
      gates[1]!({ hits: [{ score: 0.9, document_id: 'd1', text: 'the newest answer' }], search_ms: 5, embed_ms: 90 })
      await second
      gates[0]!({ hits: [{ score: 0.4, document_id: 'd1', text: 'the stale answer' }], search_ms: 9, embed_ms: 90 })
      await first
    })
    const texts = (store.getState().hits ?? []).map((h) => h.text)
    expect(texts).toEqual(['the newest answer'])
  })

  it('spends one request per press, and none for an empty question', async () => {
    /* Every keystroke used to run a search, and each one embeds the query
       through the configured endpoint -- billed, and on a client whose timeout
       is in minutes. The button is what makes a question cost once. */
    let calls = 0
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd1', source: 'handbook.md', status: 'ready' })],
      search: async () => {
        calls += 1
        return { hits: [{ score: 0.8, document_id: 'd1', text: 'the answer' }], search_ms: 6, embed_ms: 180 }
      },
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    await act(async () => {
      store.setQuery('quarterly')
    })
    expect(calls).toBe(0)

    await act(async () => {
      await store.searchNow('quarterly')
    })
    expect(calls).toBe(1)
    expect((store.getState().hits ?? []).map((h) => h.text)).toEqual(['the answer'])

    await act(async () => {
      await store.searchNow('   ')
    })
    /* Null, not empty: "asked and found nothing" and "not asked" are different
       states, and only the first one has a count to report. */
    expect(calls).toBe(1)
    expect(store.getState().hits).toBeNull()
  })



  it('lists the bases with their document counts', async () => {
    source({ bases: async () => [base({ id: 'b1', name: 'handbook', documents: 3 })] })
    await mount()
    expect(screen.getByText('handbook')).toBeTruthy()
    expect(screen.getByText('gui.kb.docs {"n":3}')).toBeTruthy()
  })

  it('shows the model a base was built with, not the one configured now', async () => {
    /* A base outlives a config change, so the page has to be able to show the
       mismatch rather than search against it silently. */
    source({
      status: async () => ({ configured: true, model: 'text-embedding-3' }),
      bases: async () => [base({ id: 'b1', embedding_model: 'bge-m3' })],
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    /* On the open base's panel rather than the rail: it is a fact about the
       base being looked at, and down a list of twenty it is noise. */
    expect(screen.getByText('bge-m3')).toBeTruthy()
  })

  it('says to configure an endpoint rather than offering a button that fails', async () => {
    source({ status: async () => ({ configured: false, model: '' }), bases: async () => [] })
    await mount()
    expect(screen.getByText('gui.kb.unconfigured')).toBeTruthy()
  })

  it('tells an empty deployment it has no bases yet', async () => {
    source()
    await mount()
    expect(screen.getByText('gui.kb.none')).toBeTruthy()
  })

  it('shows why a read failed instead of an empty list', async () => {
    /* An unreachable engine and a deployment with no bases look identical
       otherwise, and only one of them is fine. */
    source({
      bases: async () => {
        throw new Error('socket dropped')
      },
    })
    await mount()
    expect(screen.getByText('socket dropped')).toBeTruthy()
    expect(screen.queryByText('gui.kb.none')).toBeNull()
  })

  it('claims nothing before the first answer lands', async () => {
    /* Without this the first paint says "no bases" while the call is still in
       flight. */
    let release: (v: KbBase[]) => void = () => {}
    source({ bases: () => new Promise<KbBase[]>((r) => (release = r)) })
    render(<KnowledgeApp />)
    const loading = store.load()
    expect(screen.queryByText('gui.kb.none')).toBeNull()
    await act(async () => {
      release([])
      await loading
    })
    expect(screen.getByText('gui.kb.none')).toBeTruthy()
  })

  it('asks for the status and the bases together', async () => {
    /* The page cannot say "no bases yet, create one" without knowing whether
       creating one is possible at all. */
    const asked: string[] = []
    source({
      status: async () => {
        asked.push('status')
        return { configured: true, model: 'bge-m3' }
      },
      bases: async () => {
        asked.push('bases')
        return []
      },
    })
    await mount()
    expect(asked.sort()).toEqual(['bases', 'status'])
  })

  it('reports a missing source rather than throwing into the render', async () => {
    source()
    delete (window as { DS?: unknown }).DS
    await mount()
    expect(screen.getByText('no knowledge source installed')).toBeTruthy()
  })
})

describe('opening the page', () => {
  it('shows kbPage and loads', async () => {
    source()
    store.open()
    await act(async () => {
      await Promise.resolve()
    })
    expect(pages).toEqual(['kbPage'])
  })
})


describe('the write surface', () => {
  it('creates a base and reloads, so the new row is the served one', async () => {
    /* Not spliced in locally: the row the engine answers carries the model and
       the width it actually sized the collection to. */
    const made: string[] = []
    let listed: KbBase[] = []
    source({
      create: async (name: string) => {
        made.push(name)
        listed = [base({ id: 'b1', name })]
        return listed[0]!
      },
      bases: async () => listed,
    })
    await mount()
    await act(async () => {
      await store.create('handbook')
    })
    expect(made).toEqual(['handbook'])
    expect(screen.getByText('handbook')).toBeTruthy()
  })

  it('says why a create was refused instead of showing a base that is not there', async () => {
    source({
      create: async () => {
        throw new Error('connection refused')
      },
    })
    await mount()
    await act(async () => {
      await store.create('handbook')
    })
    expect(toasts()).toEqual(['connection refused'])
  })

  it('reports what the engine said, not the name of the error code', async () => {
    /* The transport rejects with the JSON-RPC error frame itself rather than an
       `Error`, so `.message` is the code's name and the sentence a reader needs
       is in `data.detail`. The test above throws a real `Error`, whose message
       is useful either way -- which is how reading only `.message` survived:
       the shape it fails on is the only shape production actually produces.

       What that cost: an embedding endpoint answering "your account balance is
       insufficient" reached the reader as the word "internal_error". */
    source({
      create: async () => {
        throw {
          code: -32603,
          message: 'internal_error',
          data: { detail: 'could not create the base: embedding endpoint returned 402' },
        }
      },
    })
    await mount()
    await act(async () => {
      await store.create('handbook')
    })
    expect(toasts()).toEqual(['could not create the base: embedding endpoint returned 402'])
  })

  it('ignores a blank name and a second click while one is in flight', async () => {
    let calls = 0
    let release: (b: KbBase) => void = () => {}
    source({
      create: (name: string) => {
        calls += 1
        return new Promise<KbBase>((r) => (release = r))
      },
    })
    await mount()
    await act(async () => {
      await store.create('   ')
    })
    expect(calls).toBe(0)
    const first = store.create('handbook')
    void store.create('handbook')
    expect(calls).toBe(1)
    await act(async () => {
      release(base({ id: 'b1' }))
      await first
    })
  })

  it('asks before deleting, and names what goes with it', async () => {
    const removed: string[] = []
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook', documents: 4 })],
      remove: async (id: string) => {
        removed.push(id)
        return {}
      },
    })
    await mount()
    await act(async () => {
      store.remove(base({ id: 'b1', name: 'handbook', documents: 4 }))
    })
    expect(confirms).toEqual(['gui.kb.delete_body {"name":"handbook","n":4}'])
    expect(removed).toEqual(['b1'])
  })

  it('opens a base and shows its documents', async () => {
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook', documents: 1 })],
      documents: async () => [
        {
          id: 'd1',
          base_id: 'b1',
          source: 'onboarding.md',
          media_type: 'text/markdown',
          size: 12,
          status: 'ready',
          chunk_count: 3,
          error: '',
          created_at: '',
          updated_at: '',
        },
      ],
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    expect(screen.getByText('onboarding.md')).toBeTruthy()
    expect(screen.getByText('gui.kb.doc_ready')).toBeTruthy()
    /* Four columns and no chunk count: the table answers "what is in this base
       and is it searchable", and how a document was cut up is what View Chunks
       is for. */
    expect(screen.getByText('gui.kb.col_updated')).toBeTruthy()
  })

  it('carries a failed document reason on its status', async () => {
    /* A failure that does not say why sends the reader to a log they may not
       have. */
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [
        {
          id: 'd1',
          base_id: 'b1',
          source: 'broken.pdf',
          media_type: 'application/pdf',
          size: 9,
          status: 'failed',
          chunk_count: 0,
          error: 'no parser for application/pdf',
          created_at: '',
          updated_at: '',
        },
      ],
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    /* On the status rather than under the row: a red line beneath every
       failure pushed the rows apart and made a list of files hard to scan, and
       the status is where a reader is already looking. */
    const status = document.querySelector('.kbtable .td.s-failed') as HTMLElement
    expect(status.getAttribute('title')).toBe('no parser for application/pdf')
    expect(document.querySelector('.kbtable .td.err')).toBeNull()
  })

  it('does not answer into a panel the reader has left', async () => {
    /* Opening b1 then b2 must not paint b1 documents under b2. */
    let release: (d: never[]) => void = () => {}
    source({
      bases: async () => [base({ id: 'b1', name: 'one' }), base({ id: 'b2', name: 'two' })],
      documents: (id: string) =>
        id === 'b1' ? new Promise((r) => (release = r as never)) : Promise.resolve([]),
    })
    await mount()
    const slow = store.open_('b1')
    await act(async () => {
      await store.open_('b2')
    })
    await act(async () => {
      release([])
      await slow
    })
    /* Both panels are on screen now, so the name is in the rail and in the
       heading: scoped to the heading, which is the one that says which base
       the documents below belong to. */
    expect(document.querySelector('.kbhd b')!.textContent).toBe('two')
    expect(screen.getByText('gui.kb.no_docs')).toBeTruthy()
  })
})


describe('documents and search', () => {
  it('queues a document, then shows it indexed', async () => {
    /* Two states on purpose: embedding outlives a request, and a panel that
       waits for it looks broken. */
    const seen: string[] = []
    source({
      bases: async () => [base({ id: 'b1' })],
      upload: async (_b: string, file: File) => {
        seen.push('upload')
        return doc({ id: 'd1', source: file.name, status: 'pending' })
      },
      index: async (id: string) => {
        seen.push('index')
        return doc({ id, status: 'ready', chunk_count: 2 })
      },
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await act(async () => {
      await store.upload(new File(['hi'], 'onboarding.md', { type: 'text/markdown' }))
    })
    expect(seen).toEqual(['upload', 'index'])
    /* Queued, then searchable: the two calls are one action to the reader, and
       the row is what tells them it finished. */
    expect(screen.getByText('gui.kb.doc_ready')).toBeTruthy()
  })

  it('says why an upload failed instead of leaving a row that is not there', async () => {
    source({
      bases: async () => [base({ id: 'b1' })],
      upload: async () => {
        throw new Error('file exceeds 20 MB limit')
      },
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await act(async () => {
      await store.upload(new File(['x'], 'huge.bin'))
    })
    expect(toasts()).toEqual(['file exceeds 20 MB limit'])
    expect(screen.getByText('gui.kb.no_docs')).toBeTruthy()
  })

  it('gives every row the same actions, and the failed one its reason', async () => {
    /* Deliberately not "only where they are the way out", which is what the
       stacked rows before this did: a table row carries one menu whatever its
       status, so reindexing a `ready` document is reachable without first
       breaking it. The reason still rides with the row that failed. */
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [
        doc({ id: 'd1', source: 'done.md', status: 'ready', chunk_count: 2 }),
        doc({ id: 'd3', source: 'broke.md', status: 'failed', error: 'endpoint said 400' }),
      ],
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    expect(document.querySelectorAll('.kbops .dots').length).toBe(2)
    await openRowMenu('done.md')
    expect(screen.getByText('gui.kb.doc_reindex')).toBeTruthy()
    expect(screen.getByText('gui.kb.delete')).toBeTruthy()
    expect(
      (document.querySelector('.kbtable .td.s-failed') as HTMLElement).getAttribute('title'),
    ).toBe('endpoint said 400')
  })

  it('indexes a stuck row again, and shows the answer', async () => {
    const asked: string[] = []
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd2', source: 'stuck.md', status: 'pending' })],
      index: async (id: string) => {
        asked.push(id)
        return doc({ id, source: 'stuck.md', status: 'ready', chunk_count: 4 })
      },
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await openRowMenu('stuck.md')
    await act(async () => {
      screen.getByText('gui.kb.doc_reindex').click()
    })
    /* The same call upload makes: `index_document` re-embeds from the stored
       blob, so an endpoint failure clears with nothing else to do. */
    expect(asked).toEqual(['d2'])
    expect(screen.getByText('gui.kb.doc_ready')).toBeTruthy()
  })

  it('puts a retried row back when the retry fails too', async () => {
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd2', source: 'stuck.md', status: 'failed', error: 'was 400' })],
      index: async () => {
        throw new Error('still 400')
      },
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await openRowMenu('stuck.md')
    await act(async () => {
      screen.getByText('gui.kb.doc_reindex').click()
    })
    /* The optimistic `indexing` was a promise the call did not keep; leaving it
       there is a row saying it is working when nothing is. */
    expect(screen.getByText('gui.kb.doc_failed')).toBeTruthy()
    expect(toasts().some((m) => m.includes('still 400'))).toBe(true)
  })

  it('will not remove a document while its index is still running', async () => {
    /* Deleting mid-index takes the record and the blob while the embed is in
       flight, and `index_document` ends by re-inserting its vectors -- into a
       collection where no record owns them. `delete_document` returns early
       once the record is gone, so nothing reclaims them, and `search` never
       joins a hit back to a record, so they keep coming back as results. */
    const gone: string[] = []
    let release: (d: KbDoc) => void = () => {}
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd2', source: 'stuck.md', status: 'failed' })],
      index: () => new Promise<KbDoc>((r) => (release = r)),
      removeDoc: async (id: string) => {
        gone.push(id)
      },
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await openRowMenu('stuck.md')
    await act(async () => {
      screen.getByText('gui.kb.doc_reindex').click()
    })
    await openRowMenu('stuck.md')
    const remove = screen.getByText('gui.kb.delete').closest('button') as HTMLButtonElement
    expect(remove.disabled).toBe(true)
    await act(async () => {
      remove.click()
    })
    expect(gone).toEqual([])
    await act(async () => {
      release(doc({ id: 'd2', source: 'stuck.md', status: 'ready', chunk_count: 1 }))
    })
    /* And it comes back once the index has landed and the row is settled --
       gone here, because a ready row needs neither button. */
    expect(screen.queryByText('gui.kb.doc_delete')).toBeNull()
  })

  it('removes a document, after asking, and re-reads what is left', async () => {
    const gone: string[] = []
    let listed = 0
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => {
        listed += 1
        return listed > 1 ? [] : [doc({ id: 'd2', source: 'stuck.md', status: 'failed' })]
      },
      removeDoc: async (id: string) => {
        gone.push(id)
      },
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await openRowMenu('stuck.md')
    await act(async () => {
      screen.getByText('gui.kb.delete').click()
    })
    await act(async () => {
      await Promise.resolve()
    })
    /* Asked first: the chunks and the stored copy go with it, and an upload is
       not always still on the reader's disk. */
    expect(confirms.some((c) => c.includes('gui.kb.doc_delete_body'))).toBe(true)
    expect(gone).toEqual(['d2'])
    expect(screen.queryByText('stuck.md')).toBeNull()
  })

  it('tells an empty result apart from not having asked', async () => {
    source({
      bases: async () => [base({ id: 'b1' })],
      search: async () => ({ hits: [], search_ms: 4, embed_ms: 120 }),
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await act(async () => {
      await store.searchNow('nothing here')
    })
    /* An empty array, not null. The distinction is the whole test: asked and
       found nothing is a result, and not having asked is not one -- and only
       the second means the panel should be showing documents. */
    expect(store.getState().hits).toEqual([])
  })

  it('drops a search that answers after the reader left the base', async () => {
    let release: (h: never[]) => void = () => {}
    source({
      bases: async () => [base({ id: 'b1', name: 'one' }), base({ id: 'b2', name: 'two' })],
      search: () => new Promise((r) => (release = r as never)),
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    const slow = store.searchNow('what')
    await act(async () => {
      store.back()
    })
    await act(async () => {
      release([])
      await slow
    })
    expect(screen.getByText('one')).toBeTruthy()
  })
})

describe('the two-panel layout', () => {
  it('keeps the bases in view while one of them is open', async () => {
    /* The whole point of the split. The drill-down this replaced put a Back
       button between a reader and the base they were comparing against, so
       both panels staying on screen is the behaviour, not decoration. */
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook' }), base({ id: 'b2', name: 'policies' })],
      documents: async () => [doc({ id: 'd1', source: 'onboarding.md', status: 'ready' })],
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    const rail = [...document.querySelectorAll('.kbrail .kbrow .nm')].map((n) => n.textContent)
    expect(rail).toEqual(['handbook', 'policies'])
    expect(document.querySelector('.kbhd b')!.textContent).toBe('handbook')
    /* And the open one is marked, since the rail is what says which base the
       panel belongs to. */
    const on = [...document.querySelectorAll('.kbrail .kbrow')].filter((r) => r.hasAttribute('aria-current'))
    expect(on.length).toBe(1)
    expect(on[0]!.textContent).toContain('handbook')
  })

  it('asks for nothing until a base is picked', async () => {
    source({ bases: async () => [base({ id: 'b1', name: 'handbook' })] })
    await mount()

    expect(screen.getByText('gui.kb.pick_base')).toBeTruthy()
    expect(document.querySelector('.kbhd')).toBeNull()
  })

  it('names the controls it has not built yet instead of pretending', async () => {
    /* Recall Test, Settings, View Chunks and Disable have nothing behind them.
       They are on screen because the design puts them there, and disabled
       because a control that looks live and does nothing when pressed is
       worse than one that says it is not ready. */
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd1', source: 'onboarding.md', status: 'ready' })],
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    await openRowMenu('onboarding.md')
    for (const label of ['gui.kb.doc_view_chunks', 'gui.kb.doc_disable']) {
      expect((screen.getByText(label).closest('button') as HTMLButtonElement).disabled).toBe(true)
    }
    /* Every data source in the menu is wired, so the button that opens it is
       not one of the stubs. */
    const add = screen.getByText('+ gui.kb.add_source').closest('button') as HTMLButtonElement
    expect(add.disabled).toBe(false)
    /* Recall Test and Settings are wired now too, so neither is a stub. */
    for (const label of ['gui.kb.recall_test', 'gui.kb.settings']) {
      const btn = screen.getByText(label).closest('button') as HTMLButtonElement
      expect(btn.disabled).toBe(false)
      expect(btn.title).toBe('')
    }
  })
})

describe('the create dialog', () => {
  const openDialog = async () => {
    await act(async () => {
      screen.getByText('+ gui.kb.new').click()
    })
  }

  it('takes a name and an embedding model, and creates with them', async () => {
    const made: Array<[string, boolean | undefined]> = []
    source({
      status: async () => ({ configured: true, model: 'bge-m3' }),
      create: async (name: string, _d: string, embedding?: boolean) => {
        made.push([name, embedding])
        return base({ id: 'b1', name })
      },
    })
    await mount()
    await openDialog()

    /* Disabled is a real choice, not the absence of one: a base nobody means
       to search by vector should not be made to carry an index. */
    const opts = [...document.querySelectorAll('#kbembed option')].map((o) => o.textContent)
    expect(opts).toEqual(['gui.kb.embed_off', 'bge-m3'])

    const field = document.getElementById('kbname') as HTMLInputElement
    await act(async () => {
      fireEvent.change(field, { target: { value: 'handbook' } })
    })
    await act(async () => {
      screen.getByText('gui.kb.create').click()
    })

    expect(made).toEqual([['handbook', true]])
    expect(document.querySelector('.kbdlg')).toBeNull()
  })

  it('makes a base with no vectors when the model is left Disabled', async () => {
    /* The bug this pins: the choice was collected and dropped, so a base asked
       for as Disabled came back carrying whatever model was configured -- and
       its panel then truthfully showed a model nobody had chosen. */
    const made: Array<[string, boolean | undefined]> = []
    source({
      status: async () => ({ configured: true, model: 'bge-m3' }),
      create: async (name: string, _d: string, embedding?: boolean) => {
        made.push([name, embedding])
        return base({ id: 'b1', name, embedding_model: '' })
      },
    })
    await mount()
    await openDialog()

    const field = document.getElementById('kbname') as HTMLInputElement
    await act(async () => {
      fireEvent.change(field, { target: { value: 't3' } })
    })
    // Disabled is the option the select opens on when nothing is picked.
    expect((document.getElementById('kbembed') as HTMLSelectElement).value).toBe('bge-m3')
    await act(async () => {
      fireEvent.change(document.getElementById('kbembed') as HTMLSelectElement, { target: { value: '' } })
    })
    await act(async () => {
      screen.getByText('gui.kb.create').click()
    })

    expect(made).toEqual([['t3', false]])
  })

  it('says Disabled on a base that has no model of its own', async () => {
    source({
      status: async () => ({ configured: true, model: 'bge-m3' }),
      bases: async () => [base({ id: 'b1', name: 't3', embedding_model: '' })],
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    expect(screen.getByText('gui.kb.embed_off')).toBeTruthy()
    expect(screen.queryByText('bge-m3')).toBeNull()
  })

  it('will not create a base with no name', async () => {
    source({ bases: async () => [] })
    await mount()
    await openDialog()

    expect((screen.getByText('gui.kb.create').closest('button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('closes on cancel without creating', async () => {
    const made: string[] = []
    source({
      create: async (name: string) => {
        made.push(name)
        return base({ id: 'b1', name })
      },
    })
    await mount()
    await openDialog()
    const field = document.getElementById('kbname') as HTMLInputElement
    await act(async () => {
      fireEvent.change(field, { target: { value: 'handbook' } })
    })
    await act(async () => {
      screen.getByText('gui.kb.cancel').click()
    })

    expect(made).toEqual([])
    expect(document.querySelector('.kbdlg')).toBeNull()
  })
})

describe('viewing the original file', () => {
  const openBase = async (docs: KbDoc[]) => {
    source({ bases: async () => [base({ id: 'b1', name: 'handbook' })], documents: async () => docs })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
  }

  const clickName = async (name: string) => {
    await act(async () => {
      ;(screen.getByText(name).closest('button') as HTMLButtonElement).click()
    })
  }

  it('frames the file when its name is clicked, and goes back', async () => {
    await openBase([doc({ id: 'd1', source: 'contract.pdf', status: 'ready' })])

    await clickName('contract.pdf')

    const frame = document.querySelector('.kbframe') as HTMLIFrameElement
    expect(frame).not.toBeNull()
    expect(frame.getAttribute('src')).toBe('/knowledge/file?document=d1')
    /* No sandbox attribute: the response already carries a CSP sandbox, and the
       attribute as well stops the browser's own PDF viewer drawing anything. */
    expect(frame.hasAttribute('sandbox')).toBe(false)
    expect(document.querySelector('.kbtable')).toBeNull()

    await act(async () => {
      ;(document.querySelector('.kbback') as HTMLButtonElement).click()
    })
    expect(document.querySelector('.kbframe')).toBeNull()
    expect(document.querySelector('.kbtable')).not.toBeNull()
  })

  it('asks the gateway to convert the formats no browser draws', async () => {
    await openBase([doc({ id: 'd2', source: 'notice.doc', status: 'ready' })])

    await clickName('notice.doc')

    /* A legacy .doc has no reader in the browser and no pure-Python one worth
       trusting, so the gateway renders it with LibreOffice first. */
    expect((document.querySelector('.kbframe') as HTMLIFrameElement).getAttribute('src')).toBe(
      '/knowledge/file?document=d2&render=pdf',
    )
  })

  it('offers a download rather than framing what cannot be drawn', async () => {
    await openBase([doc({ id: 'd3', source: 'archive.zip', status: 'ready' })])

    await clickName('archive.zip')

    expect(document.querySelector('.kbframe')).toBeNull()
    expect(screen.getByText('gui.kb.no_preview')).toBeTruthy()
    const link = screen.getByText('gui.kb.download') as HTMLAnchorElement
    expect(link.getAttribute('href')).toBe('/knowledge/file?document=d3')
    expect(link.getAttribute('download')).toBe('archive.zip')
  })

  it('leaves the viewer when the base does', async () => {
    /* A frame still showing the last base's document while the rail has moved
       on is the panel lying about what it belongs to. */
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook' }), base({ id: 'b2', name: 'policies' })],
      documents: async (id: string) =>
        id === 'b1' ? [doc({ id: 'd1', source: 'contract.pdf', status: 'ready' })] : [],
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await clickName('contract.pdf')
    expect(document.querySelector('.kbframe')).not.toBeNull()

    await act(async () => {
      await store.open_('b2')
    })

    expect(document.querySelector('.kbframe')).toBeNull()
  })

  it('renders markdown instead of framing its source', async () => {
    /* The gateway serves .md as text/plain -- correctly, it is text -- so a
       frame draws the hashes and the pipes, which is the file rather than the
       document. */
    vi.stubGlobal('fetch', async () => ({
      ok: true,
      status: 200,
      statusText: 'OK',
      text: async () => '# Onboarding\n\nRead **this** first.\n',
    }))
    await openBase([doc({ id: 'd4', source: 'onboarding.md', status: 'ready' })])

    await clickName('onboarding.md')
    await act(async () => {
      await Promise.resolve()
    })

    expect(document.querySelector('.kbframe')).toBeNull()
    const prose = document.querySelector('.kbprose') as HTMLElement
    expect(prose).not.toBeNull()
    /* h2, not h1: the shell's renderer starts headings there because the page
       owns the h1 above them. */
    expect(prose.querySelector('h2')?.textContent).toBe('Onboarding')
    expect(prose.querySelector('strong')?.textContent).toBe('this')
    vi.unstubAllGlobals()
  })

  it('escapes markup an upload put in its markdown', async () => {
    /* Rendered through dangerouslySetInnerHTML, so the escaping is the whole
       safety of it: an uploaded file is not trusted content. */
    vi.stubGlobal('fetch', async () => ({
      ok: true,
      status: 200,
      statusText: 'OK',
      text: async () => '# Hi\n\n<img src=x onerror="alert(1)">\n',
    }))
    await openBase([doc({ id: 'd5', source: 'evil.md', status: 'ready' })])

    await clickName('evil.md')
    await act(async () => {
      await Promise.resolve()
    })

    const prose = document.querySelector('.kbprose') as HTMLElement
    expect(prose.querySelector('img')).toBeNull()
    expect(prose.innerHTML).toContain('&lt;img')
    vi.unstubAllGlobals()
  })

  it('gives each file a glyph for its family, not for its extension', () => {
    /* Folded the way ragflow's own map folds xls, xlsx and csv onto one sheet
       icon: a reader scanning the column asks "document, spreadsheet or
       picture", and forty glyphs answer that no better than eight. */
    const fam = (name: string) => store.fileFamily(doc({ id: 'x', source: name }))
    expect([fam('a.doc'), fam('a.docx'), fam('a.rtf')]).toEqual(['doc', 'doc', 'doc'])
    expect([fam('a.xls'), fam('a.xlsx'), fam('a.csv')]).toEqual(['sheet', 'sheet', 'sheet'])
    expect([fam('a.ppt'), fam('a.pptx')]).toEqual(['slide', 'slide'])
    expect(fam('a.pdf')).toBe('pdf')
    expect(fam('a.md')).toBe('md')
    expect(fam('a.PNG')).toBe('image')
    expect(fam('a.json')).toBe('data')
    /* Anything unrecognised still gets a page, never a blank column. */
    expect([fam('a.zip'), fam('README')]).toEqual(['file', 'file'])
  })

  it('draws the glyph inside the control that opens the file', async () => {
    await openBase([doc({ id: 'd9', source: 'sheet.xlsx', status: 'ready' })])

    const opener = document.querySelector('.kbtable .td.nm .kbopen') as HTMLButtonElement
    expect(opener.querySelector('svg.kbico')).not.toBeNull()
    /* Inside the button, not beside it: the whole cell is the way in, and an
       icon that did nothing when clicked would be the one part that is not. */
    await act(async () => {
      ;(opener.querySelector('svg.kbico') as SVGElement).closest('button')!.click()
    })
    expect(document.querySelector('.kbframe')).not.toBeNull()
  })

  it('badges a lettered file with its own extension, not its family name', async () => {
    /* A .doc badged DOCX and an .odt badged DOC are both wrong on a row whose
       name says otherwise. */
    expect(store.formatLabel(doc({ id: 'x', source: 'a.docx' }))).toBe('DOCX')
    expect(store.formatLabel(doc({ id: 'x', source: 'a.doc' }))).toBe('DOC')
    expect(store.formatLabel(doc({ id: 'x', source: 'a.ODT' }))).toBe('ODT')
    expect(store.formatLabel(doc({ id: 'x', source: 'a.pptx' }))).toBe('PPTX')
    /* Clipped to what a badge holds, and never empty. */
    expect(store.formatLabel(doc({ id: 'x', source: 'a.markdown' }))).toBe('MARK')
    expect(store.formatLabel(doc({ id: 'x', source: 'README' }))).toBe('FILE')
  })

  it('letters the formats a reader names by extension, and draws the rest', async () => {
    await openBase([
      doc({ id: 'd1', source: 'report.docx', status: 'ready' }),
      doc({ id: 'd2', source: 'notes.md', status: 'ready' }),
      doc({ id: 'd3', source: 'photo.png', status: 'ready' }),
    ])

    const marks = [...document.querySelectorAll('.kbtable .kbico')]
    /* DOCX reads as its own name; markdown gets the mark people recognise for
       it rather than the letters MD; a picture has no extension anyone thinks
       in, so it keeps a drawn mark. */
    expect(marks[0]!.querySelector('text')?.textContent).toBe('DOCX')
    expect(marks[1]!.querySelector('text')).toBeNull()
    expect(marks[1]!.querySelector('rect')).not.toBeNull()
    expect(marks[2]!.querySelector('rect')).toBeNull()
  })

  it('tags the glyph with its family so the column reads by colour', async () => {
    /* At 15px the drawing inside the page outline is a smudge; the family
       class is what the stylesheet colours, so losing it would leave eight
       identical grey pages down the column. */
    await openBase([
      doc({ id: 'd1', source: 'budget.xlsx', status: 'ready' }),
      doc({ id: 'd2', source: 'scan.pdf', status: 'ready' }),
    ])

    const marks = [...document.querySelectorAll('.kbtable .kbico')]
    expect(marks.map((m) => m.getAttribute('class'))).toEqual([
      'kbico kbf-sheet',
      'kbico kbf-pdf',
    ])
  })

  it('sorts every offered format into framed, converted or neither', () => {
    const kind = (name: string) => store.previewKind(doc({ id: 'x', source: name }))
    expect(kind('a.pdf')).toBe('native')
    expect(kind('a.png')).toBe('native')
    expect(kind('a.md')).toBe('markdown')
    expect(kind('a.MARKDOWN')).toBe('markdown')
    expect(kind('a.docx')).toBe('converted')
    expect(kind('a.XLS')).toBe('converted')
    expect(kind('a.zip')).toBe('none')
    /* No suffix at all is not a format anyone can guess at. */
    expect(kind('README')).toBe('none')
  })
})

describe('adding a data source', () => {
  const openBase = async (over: Partial<KnowledgeSource> = {}, docs: KbDoc[] = []) => {
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook' })],
      documents: async () => docs,
      status: async () => ({ configured: true, model: 'bge-m3', extensions: ['.md', '.txt'] }),
      ...over,
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
  }

  const openSourceMenu = async () => {
    await act(async () => {
      ;(screen.getByText('+ gui.kb.add_source').closest('button') as HTMLButtonElement).click()
    })
  }

  const file = (name: string) => new File(['body'], name, { type: 'text/markdown' })

  it('offers the four kinds of data source behind one button', async () => {
    await openBase()
    await openSourceMenu()

    const items = [...document.querySelectorAll('.kbsrc .kbmenu .mi')].map((b) => b.textContent)
    expect(items).toEqual(['gui.kb.src_file', 'gui.kb.src_note', 'gui.kb.src_folder', 'gui.kb.src_url'])
  })

  it('picks a folder with the attribute that makes a chooser a folder chooser', async () => {
    /* Without `webkitdirectory` this is the file input again; it is the whole
       difference between the two menu entries. */
    await openBase()

    const inputs = [...document.querySelectorAll('.kbsrc input[type="file"]')]
    expect(inputs.map((i) => i.hasAttribute('webkitdirectory'))).toEqual([false, true])
    expect(inputs.every((i) => i.hasAttribute('multiple'))).toBe(true)
  })

  it('uploads several files one after another, not all at once', async () => {
    /* Each upload carries its bytes base64 in one websocket frame under a
       25 MB ceiling: N at once is N of those in memory and a frame ceiling
       nobody raised. */
    const order: string[] = []
    let live = 0
    let most = 0
    await openBase({
      upload: async (_b: string, f: File) => {
        live += 1
        most = Math.max(most, live)
        order.push(f.name)
        await Promise.resolve()
        live -= 1
        return doc({ id: f.name, source: f.name })
      },
      index: async (id: string) => doc({ id, source: id, status: 'ready' }),
    })

    await act(async () => {
      await store.uploadAll([file('a.md'), file('b.md'), file('c.md')])
    })

    expect(order).toEqual(['a.md', 'b.md', 'c.md'])
    expect(most).toBe(1)
  })

  it('adds the rest of a folder when one file in it fails', async () => {
    /* A folder of forty where the third is unreadable should land the other
       thirty-nine; ragflow reports the same partial success. */
    await openBase({
      upload: async (_b: string, f: File) => {
        if (f.name === 'bad.md') throw new Error('nope')
        return doc({ id: f.name, source: f.name })
      },
      index: async (id: string) => doc({ id, source: id, status: 'ready' }),
    })

    await act(async () => {
      await store.uploadAll([file('a.md'), file('bad.md'), file('c.md')])
    })

    expect(store.getState().docs.map((d) => d.source)).toEqual(['a.md', 'c.md'])
    expect(toasts()).toEqual(['nope'])
  })

  it('keeps only the files this build can index out of a folder', async () => {
    /* A source tree is mostly things no parser claims. Uploading them to watch
       them fail is not a file list. */
    await openBase()

    const kept = store.indexable([file('notes.md'), file('logo.png'), file('readme.TXT'), file('Makefile')])

    expect(kept.map((f) => f.name)).toEqual(['notes.md', 'readme.TXT'])
  })

  it('takes any file into a base that was made without an embedding model', async () => {
    /* Such a base indexes nothing at all: it keeps documents to open and to
       hand to a turn, so "what a parser claims" is not a question about it,
       and filtering by that would throw away what it is for. */
    source({
      bases: async () => [base({ id: 'b1', name: 'scratch', embedding_model: '', dimensions: 0 })],
      documents: async () => [],
      status: async () => ({ configured: true, model: 'bge-m3', extensions: ['.md'] }),
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    const kept = store.indexable([file('notes.md'), file('scan.pdf'), file('logo.png')])

    expect(kept.map((f) => f.name)).toEqual(['notes.md', 'scan.pdf', 'logo.png'])
  })

  it('says so rather than starting when a folder holds nothing indexable', async () => {
    const tried: string[] = []
    await openBase({
      upload: async (_b: string, f: File) => {
        tried.push(f.name)
        return doc({ id: f.name })
      },
    })

    await act(async () => {
      await store.uploadFolder([file('logo.png'), file('a.bin')])
    })

    expect(tried).toEqual([])
    expect(toasts()[0]).toContain('gui.kb.none_supported')
  })

  it('refuses a folder too big to take quietly', async () => {
    /* A source tree holds tens of thousands of files and each is its own
       request; starting that because somebody picked the wrong directory is
       not a thing to do without saying. */
    const tried: string[] = []
    await openBase({
      upload: async (_b: string, f: File) => {
        tried.push(f.name)
        return doc({ id: f.name })
      },
    })

    const many = Array.from({ length: store.FOLDER_MAX + 1 }, (_, i) => file(`f${i}.md`))
    await act(async () => {
      await store.uploadFolder(many)
    })

    expect(tried).toEqual([])
    expect(toasts()[0]).toContain('gui.kb.too_many')
  })

  it('writes a note as markdown and indexes it like any other document', async () => {
    const wrote: [string, string][] = []
    await openBase({
      addNote: async (_b: string, title: string, text: string) => {
        wrote.push([title, text])
        return doc({ id: 'n1', source: `${title}.md`, origin: 'note' })
      },
      index: async (id: string) => doc({ id, source: 'Plan.md', origin: 'note', status: 'ready' }),
    })
    await openSourceMenu()
    await act(async () => {
      ;(screen.getByText('gui.kb.src_note').closest('button') as HTMLButtonElement).click()
    })

    const body = document.querySelector('.kbnote') as HTMLTextAreaElement
    const title = document.getElementById('kbnotetitle') as HTMLInputElement
    await act(async () => {
      fireEvent.change(title, { target: { value: 'Plan' } })
      fireEvent.change(body, { target: { value: '# Plan\n\nship it' } })
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.create').closest('button') as HTMLButtonElement).click()
    })

    expect(wrote).toEqual([['Plan', '# Plan\n\nship it']])
    /* The dialog closes on success: one left standing over the row it just
       made is the reader wondering whether it worked. */
    expect(document.querySelector('.kbnote')).toBeNull()
  })

  it('will not save an empty note', async () => {
    await openBase()
    await openSourceMenu()
    await act(async () => {
      ;(screen.getByText('gui.kb.src_note').closest('button') as HTMLButtonElement).click()
    })

    const save = screen.getByText('gui.kb.create').closest('button') as HTMLButtonElement
    expect(save.disabled).toBe(true)
  })

  it('opens an existing note on its own text rather than on an empty box', async () => {
    /* An editor that opens empty and saves erases the note. */
    const note = doc({ id: 'n1', source: 'Plan.md', origin: 'note', status: 'ready' })
    const saved: [string, string, string][] = []
    await openBase(
      {
        updateNote: async (id: string, title: string, text: string) => {
          saved.push([id, title, text])
          return { ...note, source: `${title}.md` }
        },
        index: async (id: string) => ({ ...note, id, status: 'ready' }),
      },
      [note],
    )
    const read = vi.spyOn(store, 'readText').mockResolvedValue('# Plan\n\nship it')

    await openRowMenu('Plan.md')
    await act(async () => {
      ;(screen.getByText('gui.kb.doc_edit_note').closest('button') as HTMLButtonElement).click()
    })
    await act(async () => {
      await Promise.resolve()
    })

    const body = document.querySelector('.kbnote') as HTMLTextAreaElement
    expect(body.value).toBe('# Plan\n\nship it')
    /* The title comes back off the filename, which is what it was written to. */
    expect((document.getElementById('kbnotetitle') as HTMLInputElement).value).toBe('Plan')

    await act(async () => {
      fireEvent.change(body, { target: { value: '# Plan\n\nshipped' } })
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.note_save').closest('button') as HTMLButtonElement).click()
    })

    expect(saved).toEqual([['n1', 'Plan', '# Plan\n\nshipped']])
    read.mockRestore()
  })

  it('offers to edit a note and nothing else', async () => {
    /* Every other origin is a copy of something the reader holds elsewhere;
       editing it here would make this base the only place the change exists. */
    await openBase({}, [
      doc({ id: 'd1', source: 'handbook.md', status: 'ready' }),
      doc({ id: 'n1', source: 'Plan.md', origin: 'note', status: 'ready' }),
    ])

    await openRowMenu('handbook.md')
    expect(screen.queryByText('gui.kb.doc_edit_note')).toBeNull()

    await openRowMenu('Plan.md')
    expect(screen.queryByText('gui.kb.doc_edit_note')).not.toBeNull()
  })

  it('sends a url to the gateway to read, and closes on success', async () => {
    const asked: string[] = []
    await openBase({
      addUrl: async (_b: string, url: string) => {
        asked.push(url)
        return doc({ id: 'u1', source: 'Raven Docs.md', origin: 'url', origin_ref: url })
      },
      index: async (id: string) => doc({ id, source: 'Raven Docs.md', origin: 'url', status: 'ready' }),
    })
    await openSourceMenu()
    await act(async () => {
      ;(screen.getByText('gui.kb.src_url').closest('button') as HTMLButtonElement).click()
    })

    await act(async () => {
      fireEvent.change(document.getElementById('kburl') as HTMLInputElement, {
        target: { value: '  https://example.com/docs  ' },
      })
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.url_add').closest('button') as HTMLButtonElement).click()
    })

    expect(asked).toEqual(['https://example.com/docs'])
    expect(document.getElementById('kburl')).toBeNull()
  })

  it('says it is reading while the gateway fetches the page', async () => {
    /* The fetch runs to a 30s timeout. A dialog that only greys its own button
       out for that long reads as one that ignored the click. */
    let release: (d: KbDoc) => void = () => {}
    await openBase({
      addUrl: () => new Promise<KbDoc>((resolve) => (release = resolve)),
      index: async (id: string) => doc({ id, origin: 'url', status: 'ready' }),
    })
    await openSourceMenu()
    await act(async () => {
      ;(screen.getByText('gui.kb.src_url').closest('button') as HTMLButtonElement).click()
    })
    await act(async () => {
      fireEvent.change(document.getElementById('kburl') as HTMLInputElement, {
        target: { value: 'https://example.com/docs' },
      })
    })

    expect(document.querySelector('.kbwait')).toBeNull()
    await act(async () => {
      ;(screen.getByText('gui.kb.url_add').closest('button') as HTMLButtonElement).click()
    })

    const wait = document.querySelector('.kbwait') as HTMLElement
    expect(wait.textContent).toContain('gui.kb.url_reading')
    /* Announced, not only drawn. */
    expect(wait.getAttribute('role')).toBe('status')
    expect(wait.querySelector('.kbring')).not.toBeNull()
    /* And the address cannot be edited out from under the request in flight. */
    expect((document.getElementById('kburl') as HTMLInputElement).disabled).toBe(true)

    await act(async () => {
      release(doc({ id: 'u1', source: 'Docs.md', origin: 'url' }))
      await Promise.resolve()
    })
    expect(document.querySelector('.kbwait')).toBeNull()
  })

  it('closes the dialog as soon as the row exists, not when indexing ends', async () => {
    /* From the moment there is a row, the row's own status column is what
       reports the embedding -- holding the reader in front of a modal until it
       finishes tells them nothing the list is not already showing. */
    let finishIndex: (d: KbDoc) => void = () => {}
    await openBase({
      addUrl: async () => doc({ id: 'u1', source: 'Docs.md', origin: 'url', status: 'pending' }),
      index: () => new Promise<KbDoc>((resolve) => (finishIndex = resolve)),
    })
    await openSourceMenu()
    await act(async () => {
      ;(screen.getByText('gui.kb.src_url').closest('button') as HTMLButtonElement).click()
    })
    await act(async () => {
      fireEvent.change(document.getElementById('kburl') as HTMLInputElement, {
        target: { value: 'https://example.com/docs' },
      })
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.url_add').closest('button') as HTMLButtonElement).click()
      await Promise.resolve()
    })

    expect(document.getElementById('kburl')).toBeNull()
    expect(store.getState().docs.map((d) => d.status)).toEqual(['pending'])

    await act(async () => {
      finishIndex(doc({ id: 'u1', source: 'Docs.md', origin: 'url', status: 'ready' }))
      await Promise.resolve()
    })
    expect(store.getState().docs.map((d) => d.status)).toEqual(['ready'])
  })

  it('leaves the url dialog open when the page could not be read', async () => {
    /* The address is usually nearly right; throwing it away with the dialog
       means typing it again. */
    await openBase({
      addUrl: async () => {
        throw new Error('the reader answered HTTP 404')
      },
    })
    await openSourceMenu()
    await act(async () => {
      ;(screen.getByText('gui.kb.src_url').closest('button') as HTMLButtonElement).click()
    })
    await act(async () => {
      fireEvent.change(document.getElementById('kburl') as HTMLInputElement, {
        target: { value: 'https://example.com/gone' },
      })
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.url_add').closest('button') as HTMLButtonElement).click()
    })

    expect(toasts()).toEqual(['the reader answered HTTP 404'])
    expect(document.getElementById('kburl')).not.toBeNull()
  })

  it('names a row by where it came from, not by the format it is stored in', async () => {
    /* A note and a captured page are both markdown on disk. A column of "File"
       against all three answers nothing. */
    await openBase({}, [
      doc({ id: 'd1', source: 'handbook.md', status: 'ready' }),
      doc({ id: 'n1', source: 'Plan.md', origin: 'note', status: 'ready' }),
      doc({ id: 'u1', source: 'Docs.md', origin: 'url', origin_ref: 'https://x', status: 'ready' }),
    ])

    const rows = [...document.querySelectorAll('.kbtable .td.nm')]
    const types = rows.map((r) => r.nextElementSibling?.textContent)
    expect(types).toEqual(['gui.kb.doc_type_file', 'gui.kb.doc_type_note', 'gui.kb.doc_type_url'])
    /* And each gets its own glyph, for the same reason. */
    const marks = [...document.querySelectorAll('.kbtable .kbico')].map((m) => m.getAttribute('class'))
    expect(marks).toEqual(['kbico kbf-md', 'kbico kbf-note', 'kbico kbf-link'])
  })

  it('keeps a captured page pointing at the address it was read from', async () => {
    /* A copy taken once. Without the address, a reader looking at a stale copy
       has no way back to the live page. */
    const page = doc({
      id: 'u1',
      source: 'Docs.md',
      origin: 'url',
      origin_ref: 'https://example.com/docs',
      status: 'ready',
    })
    await openBase({}, [page])
    const read = vi.spyOn(store, 'readText').mockResolvedValue('# Docs')

    await act(async () => {
      ;(screen.getByText('Docs.md').closest('button') as HTMLButtonElement).click()
    })

    const link = document.querySelector('.kbvhd .kbfrom') as HTMLAnchorElement
    expect(link.href).toBe('https://example.com/docs')
    /* Someone else's page: it does not get this one's referrer, and it opens
       away from the app rather than replacing it. */
    expect(link.rel).toBe('noopener noreferrer')
    expect(link.target).toBe('_blank')
    read.mockRestore()
  })

  it('shows the drop target only while something is being dragged over it', async () => {
    await openBase()
    expect(document.querySelector('.kbdrop')).toBeNull()

    const pane = document.querySelector('.kbpane') as HTMLElement
    await act(async () => {
      fireEvent.dragEnter(pane, { dataTransfer: { types: ['Files'] } })
    })
    expect(document.querySelector('.kbdrop')).not.toBeNull()

    /* Counted rather than set: dragging across a child fires leave on the
       parent, and a boolean would flicker the highlight off mid-drag. */
    await act(async () => {
      fireEvent.dragEnter(pane, { dataTransfer: { types: ['Files'] } })
      fireEvent.dragLeave(pane)
    })
    expect(document.querySelector('.kbdrop')).not.toBeNull()

    await act(async () => {
      fireEvent.dragLeave(pane)
    })
    expect(document.querySelector('.kbdrop')).toBeNull()
  })

  it('takes dropped files through the same filter a picked folder goes through', async () => {
    const tried: string[] = []
    await openBase({
      upload: async (_b: string, f: File) => {
        tried.push(f.name)
        return doc({ id: f.name, source: f.name })
      },
      index: async (id: string) => doc({ id, source: id, status: 'ready' }),
    })

    const pane = document.querySelector('.kbpane') as HTMLElement
    await act(async () => {
      fireEvent.drop(pane, { dataTransfer: { files: [file('notes.md'), file('logo.png')], items: [] } })
    })
    await act(async () => {
      await Promise.resolve()
    })

    expect(tried).toEqual(['notes.md'])
    expect(document.querySelector('.kbdrop')).toBeNull()
  })
})

describe('the recall test', () => {
  const HITS: KbSearch = {
    hits: [
      { score: 0.8123, document_id: 'd1', text: 'the nearest passage', chunk_index: 7, total_chunks: 12, source: 'handbook.md' },
      { score: 0.4011, document_id: 'gone', text: 'from a deleted row', chunk_index: 0, total_chunks: 3, source: 'old.md' },
    ],
    search_ms: 6,
    embed_ms: 182.4,
  }

  const openPanel = async (over: Partial<KnowledgeSource> = {}, over2: Partial<KbBase> = {}) => {
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook', ...over2 })],
      documents: async () => [doc({ id: 'd1', source: 'handbook.md', status: 'ready' })],
      ...over,
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    const button = screen.getByText('gui.kb.recall_test').closest('button') as HTMLButtonElement
    if (!button.disabled) {
      await act(async () => {
        button.click()
      })
    }
    return button
  }

  const ask = async (q: string) => {
    const field = document.querySelector('.kbask .kbname') as HTMLInputElement
    await act(async () => {
      fireEvent.change(field, { target: { value: q } })
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.recall_run').closest('button') as HTMLButtonElement).click()
      await Promise.resolve()
    })
  }

  afterEach(() => {
    try {
      localStorage.clear()
    } catch {
      /* not every environment has one */
    }
  })

  it('reports the count that came back, not the limit that was asked for', async () => {
    await openPanel({ search: async () => HITS })
    await ask('what is the leave policy')

    expect((document.querySelector('.kbstats b') as HTMLElement).textContent).toBe('gui.kb.recall_n {"n":2}')
    /* The index's own time, not the round trip to the embedding endpoint --
       that one is an order of magnitude larger and describes the provider. */
    expect((document.querySelector('.kbstats span') as HTMLElement).textContent).toBe(
      'gui.kb.recall_ms {"ms":6}',
    )
    expect((document.querySelector('.kbstats span') as HTMLElement).title).toContain('182.4')
  })

  it('asks for as many chunks as the base is configured for', async () => {
    const asked: Array<number | undefined> = []
    await openPanel(
      {
        search: async (_b: string[], _q: string, topK?: number) => {
          asked.push(topK)
          return HITS
        },
      },
      { top_k: 9 },
    )
    await ask('anything')

    /* The base's own Top K, so the slider in its settings is visibly the thing
       that decides what comes back. */
    expect(asked).toEqual([9])
  })

  it('shows the score beside the rank, and where in its document the chunk sat', async () => {
    await openPanel({ search: async () => HITS })
    await ask('what is the leave policy')

    const first = document.querySelector('.kbhit') as HTMLElement
    expect((first.querySelector('.kbhitsc') as HTMLElement).textContent).toBe('0.812')
    expect((first.querySelector('.kbhitrk') as HTMLElement).textContent).toBe('gui.kb.recall_rank {"n":1}')
    /* One-based for a reader: chunk_index 7 is the eighth piece. */
    expect((first.querySelector('.kbhitix') as HTMLElement).textContent).toBe('#8')
    expect((first.querySelector('.kbhitix') as HTMLElement).title).toContain('"total":12')
  })

  it('opens the nearest hit and leaves the rest folded', async () => {
    /* Ten chunks of prose at once is a wall, not a list: the reader is
       scanning for which document answered before reading any of it. */
    await openPanel({ search: async () => HITS })
    await ask('what is the leave policy')

    expect(document.querySelectorAll('.kbhittx').length).toBe(1)
    expect((document.querySelector('.kbhittx') as HTMLElement).textContent).toBe('the nearest passage')

    const second = [...document.querySelectorAll('.kbhithd')][1] as HTMLButtonElement
    await act(async () => {
      second.click()
    })
    expect(document.querySelectorAll('.kbhittx').length).toBe(2)
  })

  it('names a hit whose document is no longer in the list', async () => {
    /* A search answers from the index, and a row deleted since is still in it
       until the next write. The hit carries its own source for exactly this. */
    await openPanel({ search: async () => HITS })
    await ask('what is the leave policy')

    const names = [...document.querySelectorAll('.kbhitnm')].map((e) => e.textContent)
    expect(names).toEqual(['handbook.md', 'old.md'])
  })

  it('tells nothing found apart from nothing asked', async () => {
    await openPanel({ search: async () => ({ hits: [], search_ms: 4, embed_ms: 120 }) })

    expect(screen.getByText('gui.kb.recall_empty')).toBeTruthy()
    await ask('nothing like this')
    expect(screen.getByText('gui.kb.recall_none')).toBeTruthy()
    expect(document.querySelector('.kbstats b')!.textContent).toBe('gui.kb.recall_n {"n":0}')
  })

  it('will not offer a recall test on a base that has no vectors', async () => {
    /* `search` skips such a base rather than failing, which from here would
       look like a base that answers nothing to every question. */
    const button = await openPanel({}, { embedding_model: '', dimensions: 0 })

    expect(button.disabled).toBe(true)
    expect(button.title).toBe('gui.kb.recall_off')
    expect(document.querySelector('.kbask')).toBeNull()
  })

  it('remembers what this browser has asked, newest first and without repeats', async () => {
    await openPanel({ search: async () => HITS })
    await ask('first question')
    await ask('second question')
    await ask('first question')

    expect(store.history()).toEqual(['first question', 'second question'])
  })

  it('does not remember a question the search never answered', async () => {
    let fail = true
    await openPanel({
      search: async () => {
        if (fail) throw new Error('endpoint said 401')
        return HITS
      },
    })
    await ask('a question that failed')
    expect(toasts()).toEqual(['endpoint said 401'])
    expect(store.history()).toEqual([])

    fail = false
    await ask('a question that worked')
    expect(store.history()).toEqual(['a question that worked'])
  })

  it('puts a remembered question back in the box and runs it', async () => {
    const asked: string[] = []
    await openPanel({
      search: async (_b: string[], q: string) => {
        asked.push(q)
        return HITS
      },
    })
    await ask('what is the leave policy')

    await act(async () => {
      ;(document.querySelector('.kbpast') as HTMLButtonElement).click()
    })
    await act(async () => {
      ;(screen.getByText('what is the leave policy').closest('button') as HTMLButtonElement).click()
      await Promise.resolve()
    })

    expect(asked).toEqual(['what is the leave policy', 'what is the leave policy'])
    expect((document.querySelector('.kbask .kbname') as HTMLInputElement).value).toBe(
      'what is the leave policy',
    )
  })

  it('forgets the history when asked to', async () => {
    await openPanel({ search: async () => HITS })
    await ask('something private')
    expect(store.history()).toEqual(['something private'])

    await act(async () => {
      ;(document.querySelector('.kbpast') as HTMLButtonElement).click()
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.recall_forget').closest('button') as HTMLButtonElement).click()
    })

    expect(store.history()).toEqual([])
  })

  it('survives a history that storage hands back as something else', async () => {
    /* localStorage is writable by anything on the origin, and one number in
       that array renders as a blank row and throws on `.trim()`. */
    localStorage.setItem('raven.gui.kbq', JSON.stringify(['ok', 42, null, '  ']))
    expect(store.history()).toEqual(['ok'])

    localStorage.setItem('raven.gui.kbq', '{not json')
    expect(store.history()).toEqual([])
  })
})

describe('the knowledge base settings', () => {
  const openSettings = async (over: Partial<KnowledgeSource> = {}, on: Partial<KbBase> = {}) => {
    const saved: Array<Record<string, unknown>> = []
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook', ...on })],
      documents: async () => [doc({ id: 'd1', status: 'ready' })],
      settings: async (_b: string, values) => {
        saved.push(values as Record<string, unknown>)
        return base({ id: 'b1', name: 'handbook', ...on, ...values })
      },
      ...over,
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.settings').closest('button') as HTMLButtonElement).click()
    })
    return saved
  }

  const field = (label: string): HTMLElement =>
    (screen.getByText(label).closest('.kbset') as HTMLElement)

  it('shows the fields the design asks for, and no rerank model', async () => {
    await openSettings()

    for (const label of ['gui.kb.set_proc', 'gui.kb.set_embed', 'gui.kb.set_topk', 'gui.kb.set_smart', 'gui.kb.set_sep', 'gui.kb.set_size', 'gui.kb.set_lap']) {
      expect(screen.getByText(label)).toBeTruthy()
    }
    expect(screen.queryByText('gui.kb.set_rerank')).toBeNull()
    expect(document.body.textContent).not.toContain('Rerank')
  })

  it('gives every setting a sentence saying what it does', async () => {
    /* A dialog of nouns -- Top K, Overlap Size -- tells a reader nothing about
       what moving them costs. */
    await openSettings()

    const helps = [...document.querySelectorAll('.kbsets .kbhelp')]
    expect(helps.length).toBe(7)
    for (const help of helps) {
      expect(help.getAttribute('title')).toBeTruthy()
      /* Reachable without a pointer, or the sentence only exists for people
         who can hover. */
      expect(help.getAttribute('tabindex')).toBe('0')
      expect(help.getAttribute('aria-label')).toBe(help.getAttribute('title'))
    }
  })

  it('shows the embedding model without offering to change it', async () => {
    /* The store is sized to its vector width when the base is created, so a
       dropdown here would be a way to invalidate every vector in the base. */
    await openSettings({}, { embedding_model: 'BAAI/bge-large-zh-v1.5' })

    const row = field('gui.kb.set_embed')
    expect(row.textContent).toContain('BAAI/bge-large-zh-v1.5')
    expect(row.querySelector('select')).toBeNull()
    expect(row.querySelector('input')).toBeNull()
  })

  it('lists the processors it will offer, every one of them unavailable', async () => {
    /* The list is the answer to "what will this eventually do"; a lone "Don't
       use" in a select answers nothing. Nothing is wired, so every row says
       why it cannot be picked rather than looking like a choice. */
    await openSettings()

    expect((field('gui.kb.set_proc').querySelector('.kbpick') as HTMLElement).textContent).toContain(
      'gui.kb.set_proc_off',
    )
    await act(async () => {
      ;(document.querySelector('.kbpick') as HTMLButtonElement).click()
    })

    const rows = [...document.querySelectorAll('.kbprocr')]
    expect(rows.map((r) => (r.querySelector('.nm') as HTMLElement).textContent)).toEqual([
      'gui.kb.proc_local',
      'PaddleOCR',
      'MinerU',
      'Doc2X',
      'Mistral',
      'gui.kb.proc_settings',
      'gui.kb.set_proc_off',
    ])
    expect(rows.every((r) => r.getAttribute('aria-disabled') === 'true')).toBe(true)
    /* Ours is a download away; the rest are other people's services and want a key. */
    expect((rows[0]!.querySelector('.st') as HTMLElement).textContent).toBe('gui.kb.proc_notdl')
    expect((rows[1]!.querySelector('.st') as HTMLElement).textContent).toBe('gui.kb.proc_notcfg')
    /* And the list says which one is actually in force. */
    expect(rows[6]!.getAttribute('aria-selected')).toBe('true')
  })

  it('wears the raven mark for the processor that is ours', async () => {
    await openSettings()
    await act(async () => {
      ;(document.querySelector('.kbpick') as HTMLButtonElement).click()
    })

    const mine = document.querySelector('.kbprocr .provider-icon') as HTMLImageElement
    expect(mine.getAttribute('src')).toContain('assets/raven.svg')
    /* Named, so the dark-mode rule can spare it the inversion every monochrome
       vendor mark gets: it is a gradient, and inverting it prints the wrong
       colours rather than a legible drawing. */
    expect(mine.getAttribute('data-provider')).toBe('raven')
  })

  it('opens on what the base is set to, and saves what was moved', async () => {
    const saved = await openSettings({}, { top_k: 12, chunk_size: 1024, chunk_overlap: 200 })

    const slider = field('gui.kb.set_topk').querySelector('input[type="range"]') as HTMLInputElement
    expect(slider.value).toBe('12')
    expect((field('gui.kb.set_size').querySelector('input') as HTMLInputElement).value).toBe('1024')
    expect((field('gui.kb.set_lap').querySelector('input') as HTMLInputElement).value).toBe('200')

    await act(async () => {
      fireEvent.change(slider, { target: { value: '7' } })
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.set_save').closest('button') as HTMLButtonElement).click()
      await Promise.resolve()
    })

    expect(saved[0]!.top_k).toBe(7)
    /* Closed and the row updated: the base in the list is what the panel
       behind this dialog is drawn from. */
    expect(document.querySelector('.kbsets')).toBeNull()
    expect(store.getState().bases[0]!.top_k).toBe(7)
  })

  it('shows a base that predates these settings as the defaults it behaves as', async () => {
    await openSettings({}, { top_k: undefined, chunk_size: undefined })

    expect((field('gui.kb.set_topk').querySelector('input') as HTMLInputElement).value).toBe(
      String(store.DEFAULTS.top_k),
    )
    expect((field('gui.kb.set_size').querySelector('input') as HTMLInputElement).value).toBe(
      String(store.DEFAULTS.chunk_size),
    )
    expect(store.DEFAULTS.top_k).toBe(6)
  })

  it('escapes the separator so two newlines can be typed into one line', async () => {
    const saved = await openSettings({}, { smart_chunking: false, separator: '\n\n' })

    const input = field('gui.kb.set_sep').querySelector('input') as HTMLInputElement
    expect(input.value).toBe('\\n\\n')

    await act(async () => {
      fireEvent.change(input, { target: { value: '\\n---\\n' } })
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.set_save').closest('button') as HTMLButtonElement).click()
      await Promise.resolve()
    })

    expect(saved[0]!.separator).toBe('\n---\n')
  })

  it('will not save an overlap that swallows the chunk it overlaps', async () => {
    /* Every chunk would contain the whole of the one before it. Refused here
       as well as by the engine, because a disabled Save says which of the two
       numbers is wrong while a toast after the fact does not. */
    await openSettings({}, { chunk_size: 512, chunk_overlap: 50 })

    const save = screen.getByText('gui.kb.set_save').closest('button') as HTMLButtonElement
    expect(save.disabled).toBe(false)

    await act(async () => {
      fireEvent.change(field('gui.kb.set_lap').querySelector('input')!, { target: { value: '512' } })
    })
    expect(save.disabled).toBe(true)

    /* And an emptied field is mid-edit, not a zero. */
    await act(async () => {
      fireEvent.change(field('gui.kb.set_size').querySelector('input')!, { target: { value: '' } })
    })
    expect(save.disabled).toBe(true)
  })

  it('puts the defaults back without saving them', async () => {
    const saved = await openSettings({}, { top_k: 40, chunk_size: 2048, chunk_overlap: 400 })

    await act(async () => {
      ;(screen.getByText('gui.kb.set_restore').closest('button') as HTMLButtonElement).click()
    })

    expect((field('gui.kb.set_topk').querySelector('input') as HTMLInputElement).value).toBe(
      String(store.DEFAULTS.top_k),
    )
    expect((field('gui.kb.set_size').querySelector('input') as HTMLInputElement).value).toBe(
      String(store.DEFAULTS.chunk_size),
    )
    expect((field('gui.kb.set_lap').querySelector('input') as HTMLInputElement).value).toBe(
      String(store.DEFAULTS.chunk_overlap),
    )
    /* Restoring is an edit like any other: nothing is written until Save. */
    expect(saved).toEqual([])
  })

  it('toggles smart chunking and disables the separator it replaces', async () => {
    const saved = await openSettings({}, { smart_chunking: true })

    const toggle = field('gui.kb.set_smart').querySelector('.kbtog') as HTMLButtonElement
    expect(toggle.getAttribute('aria-checked')).toBe('true')
    /* Nothing plainly splits on a separator while the structure is doing it. */
    expect((field('gui.kb.set_sep').querySelector('input') as HTMLInputElement).disabled).toBe(true)

    await act(async () => {
      toggle.click()
    })
    expect(toggle.getAttribute('aria-checked')).toBe('false')
    expect((field('gui.kb.set_sep').querySelector('input') as HTMLInputElement).disabled).toBe(false)

    await act(async () => {
      ;(screen.getByText('gui.kb.set_save').closest('button') as HTMLButtonElement).click()
      await Promise.resolve()
    })
    expect(saved[0]!.smart_chunking).toBe(false)
  })

  it('says that chunking changes reach only what is added next', async () => {
    /* The chunks already in the base were cut by the old numbers and stay that
       way until they are indexed again, which is the question a reader has the
       moment they move these. */
    await openSettings()

    expect(screen.getByText('gui.kb.set_newonly')).toBeTruthy()
  })
})

describe('picking several files at once', () => {
  const THREE = [
    doc({ id: 'd1', source: 'deck.pptx', status: 'ready' }),
    doc({ id: 'd2', source: 'report.docx', status: 'ready' }),
    doc({ id: 'd3', source: 'notes.md', status: 'failed', error: 'endpoint said 400' }),
  ]

  const openWith = async (over: Partial<KnowledgeSource> = {}, docs: KbDoc[] = THREE) => {
    /* `show` drops a toast when the page's standing host is absent, and
       whether a previous test left one behind is not what these assert. */
    toastHost()
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook' })],
      documents: async () => docs,
      ...over,
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
  }

  const ticks = (): HTMLInputElement[] =>
    [...document.querySelectorAll('.kbtable .td.kbtick input')] as HTMLInputElement[]
  const headTick = (): HTMLInputElement =>
    document.querySelector('.kbtable .th.kbtick input') as HTMLInputElement
  const tick = async (at: number) => {
    await act(async () => {
      fireEvent.click(ticks()[at]!)
    })
  }
  /* The buttons start work they do not await, so a test that asserts on its
     outcome has to let it finish. */
  const settle = async () => {
    await act(async () => {
      for (let i = 0; i < 20; i += 1) await Promise.resolve()
    })
  }

  it('offers the two actions only once something is picked', async () => {
    /* The row says what the base is until there is a selection to act on;
       both at once would put two counts and four controls on one line. */
    await openWith()
    expect(document.querySelector('.kbpicked')).toBeNull()
    expect(screen.getByText('+ gui.kb.add_source')).toBeTruthy()

    await tick(0)

    expect(document.querySelector('.kbpicked')!.textContent).toContain('gui.kb.picked_n {"n":1}')
    expect(screen.getByText('gui.kb.docs_reindex')).toBeTruthy()
    expect(screen.getByText('gui.kb.delete')).toBeTruthy()
    /* And adding a source is not what a reader reaches for mid-selection. */
    expect(screen.queryByText('+ gui.kb.add_source')).toBeNull()
  })

  it('marks a picked row across its whole width', async () => {
    /* The table is a grid, so a row is six sibling cells rather than an
       element that could carry the state; marking only some would stripe it. */
    await openWith()
    await tick(1)

    const marked = [...document.querySelectorAll('.kbtable .td.kbsel')]
    expect(marked.length).toBe(6)
    expect(marked.map((c) => c.textContent).join('')).toContain('report.docx')
  })

  it('ticks every row from the header, and clears from it', async () => {
    await openWith()

    await act(async () => {
      fireEvent.click(headTick())
    })
    expect(ticks().every((t) => t.checked)).toBe(true)
    expect(headTick().checked).toBe(true)

    /* Pressing it again clears rather than re-picking, which is what every
       list of checkboxes does. */
    await act(async () => {
      fireEvent.click(headTick())
    })
    expect(ticks().some((t) => t.checked)).toBe(false)
    expect(document.querySelector('.kbpicked')).toBeNull()
  })

  it('leaves the header tick off until every row is on', async () => {
    await openWith()

    await tick(0)
    expect(headTick().checked).toBe(false)
    await tick(1)
    await tick(2)
    expect(headTick().checked).toBe(true)

    await tick(1)
    expect(headTick().checked).toBe(false)
  })

  it('offers nothing to pick when there is nothing in the base', async () => {
    /* No table at all rather than a header tick over an empty list, which
       would offer to act on nothing. */
    await openWith({}, [])

    expect(document.querySelector('.kbtable')).toBeNull()
    expect(screen.getByText('gui.kb.no_docs')).toBeTruthy()
  })

  it('indexes the picked rows one at a time, and reports one that fails', async () => {
    /* Each embeds its chunks through the configured endpoint, so twenty at
       once is twenty of those against a rate limit nobody raised. */
    const order: string[] = []
    let live = 0
    let most = 0
    await openWith({
      index: async (id: string) => {
        live += 1
        most = Math.max(most, live)
        order.push(id)
        await Promise.resolve()
        live -= 1
        if (id === 'd2') throw new Error('endpoint said 429')
        return doc({ id, status: 'ready', chunk_count: 2 })
      },
    })

    await act(async () => {
      fireEvent.click(headTick())
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.docs_reindex').closest('button') as HTMLButtonElement).click()
    })
    await settle()

    expect(order).toEqual(['d1', 'd2', 'd3'])
    expect(most).toBe(1)
    /* One failing does not stop the rest, and the toast names it. */
    expect(toasts()).toEqual(['endpoint said 429'])
    const byId = Object.fromEntries(store.getState().docs.map((d) => [d.id, d.status]))
    /* d3 was failed and is now ready, which is what a reindex is for. d2's
       row goes back to what it was rather than keeping the optimistic
       `indexing` this started with -- the same thing one row's retry does,
       because the call failing says nothing about the document. */
    expect([byId.d1, byId.d2, byId.d3]).toEqual(['ready', 'ready', 'ready'])
  })

  it('asks once for the whole selection, naming how many', async () => {
    /* Twenty confirmations is a dialog a reader clicks through without
       reading, which is worse than one that names the number. */
    const removed: string[] = []
    /* The list answers with what is left, the way the engine would: without
       that the reload after the delete puts the rows back. */
    await openWith({
      documents: async () => THREE.filter((d) => !removed.includes(d.id)),
      removeDoc: async (id: string) => {
        removed.push(id)
      },
    })

    await tick(0)
    await tick(1)
    await act(async () => {
      ;(screen.getByText('gui.kb.delete').closest('button') as HTMLButtonElement).click()
    })
    await settle()

    expect(confirms).toEqual(['gui.kb.docs_delete_body {"count":2}'])
    expect(removed.sort()).toEqual(['d1', 'd2'])
    /* Off the list, and un-ticked with them. */
    expect(store.getState().picked).toEqual([])
    expect(screen.queryByText('deck.pptx')).toBeNull()
    expect(screen.getByText('notes.md')).toBeTruthy()
  })

  it('drops ticks for rows that are no longer in the list', async () => {
    /* They would keep counting towards the number and towards what the two
       buttons act on. */
    let listed = THREE
    await openWith({
      documents: async () => listed,
      removeDoc: async () => {},
    })

    await act(async () => {
      fireEvent.click(headTick())
    })
    expect(store.getState().picked.length).toBe(3)

    listed = [THREE[2]!]
    await act(async () => {
      await store.open_('b1')
    })
    expect(store.getState().picked).toEqual([])
  })

  it('forgets the selection when another base is opened', async () => {
    await openWith()
    await tick(0)
    expect(store.getState().picked).toEqual(['d1'])

    await act(async () => {
      store.back()
    })
    expect(store.getState().picked).toEqual([])
  })
})
