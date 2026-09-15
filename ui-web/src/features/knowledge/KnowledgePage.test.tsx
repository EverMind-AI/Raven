// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { KnowledgeApp } from './KnowledgePage'
import * as store from './store'

import type { KbBase, KbDoc, KnowledgeSource } from './types'
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
      search: async () => [],
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
  const row = [...document.querySelectorAll('.kbtable .td.nm')].find((c) => c.textContent === name)
  const dots = row!.parentElement!.querySelector('.kbops .dots') as HTMLButtonElement
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

  /* The hits UI left with the search box: the panel is a file table now, and
     Recall Test is where searching comes back. What survived the move is the
     store logic underneath -- the request token and the debounce -- so these
     assert what the store holds rather than what is painted, which is also
     where the races they pin actually live. A test that drove the removed
     input would have to be deleted; one that drives the store does not. */
  it('keeps the newest answer when an older one lands after it', async () => {
    /* Both requests are for the same base, so the openId guard passes for each:
       without a request token the slower prefix repainted the panel under the
       text the reader had finished typing. */
    const gates: Array<(h: Array<{ score: number; document_id: string; text: string }>) => void> = []
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
      gates[1]!([{ score: 0.9, document_id: 'd1', text: 'the newest answer' }])
      await second
      gates[0]!([{ score: 0.4, document_id: 'd1', text: 'the stale answer' }])
      await first
    })
    const texts = (store.getState().hits ?? []).map((h) => h.text)
    expect(texts).toEqual(['the newest answer'])
  })

  it('clears the hits through the debounced path when the box is emptied', async () => {
    /* The four cases above moved onto `searchNow`, which left `search`'s own
       empty-query branch with no cover: stale hits sitting over the document
       list after the reader clears the field would ship green. */
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd1', source: 'handbook.md', status: 'ready' })],
      search: async () => [{ score: 0.8, document_id: 'd1', text: 'the answer' }],
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await act(async () => {
      await store.searchNow('what')
    })
    expect((store.getState().hits ?? []).map((h) => h.text)).toEqual(['the answer'])
    await act(async () => {
      store.search('')
    })
    /* Null, not empty: "asked and found nothing" and "not asked" are different
       states, and the panel shows the documents again only for the second. */
    expect(store.getState().hits).toBeNull()
    expect(screen.getByText('handbook.md')).toBeTruthy()
  })

  it('answers Enter without waiting out the debounce', async () => {
    let calls = 0
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd1', status: 'ready' })],
      search: async () => {
        calls += 1
        return [{ score: 0.5, document_id: 'd1', text: 'straight away' }]
      },
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    /* The box this used to type into is gone. What it pinned is still real:
       a debounced keystroke followed by an immediate ask must spend one
       request, not two, and must not leave the timer behind to fire a second. */
    await act(async () => {
      store.search('quarterly')
    })
    expect(calls).toBe(0)
    await act(async () => {
      await store.searchNow('quarterly')
    })
    expect((store.getState().hits ?? []).map((h) => h.text)).toEqual(['straight away'])
    /* One request, not two: Enter cancels the keystroke's pending timer rather
       than racing it. */
    expect(calls).toBe(1)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 400))
    })
    expect(calls).toBe(1)
  })

  it('spends one request for a burst of keystrokes, not one each', async () => {
    let calls = 0
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd1', status: 'ready' })],
      search: async () => {
        calls += 1
        return []
      },
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    for (const q of ['q', 'qu', 'qua', 'quar']) store.search(q)
    expect(calls).toBe(0)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 400))
    })
    /* Four keystrokes, one embedding request. */
    expect(calls).toBe(1)
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

  it('carries a failed document reason on its own row', async () => {
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
    expect(screen.getByText('no parser for application/pdf')).toBeTruthy()
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

  it('shows hits for a query and the documents again when it is cleared', async () => {
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd1', status: 'ready' })],
      search: async () => [{ score: 0.8123, document_id: 'd1', text: 'the answer' }],
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await act(async () => {
      await store.searchNow('what')
    })
    expect((store.getState().hits ?? []).map((h) => h.text)).toEqual(['the answer'])
    await act(async () => {
      await store.searchNow('  ')
    })
    /* Whitespace is not a query: it clears rather than asking, and the panel
       is back to the documents. */
    expect(store.getState().hits).toBeNull()
    expect(screen.getByText('onboarding.md')).toBeTruthy()
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
    expect(screen.getByText('endpoint said 400')).toBeTruthy()
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
    source({ bases: async () => [base({ id: 'b1' })], search: async () => [] })
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

    for (const label of ['gui.kb.recall_test', 'gui.kb.settings']) {
      const btn = screen.getByText(label).closest('button') as HTMLButtonElement
      expect(btn.disabled).toBe(true)
      expect(btn.title).toBe('gui.kb.soon')
    }
    await openRowMenu('onboarding.md')
    for (const label of ['gui.kb.doc_view_chunks', 'gui.kb.doc_disable']) {
      expect((screen.getByText(label).closest('button') as HTMLButtonElement).disabled).toBe(true)
    }
    /* Adding a file is the one data source the engine already takes, so that
       button is wired rather than stubbed beside a working upload. */
    const add = screen.getByText('+ gui.kb.add_source').closest('label')!
    expect(add.querySelector('input[type="file"]')).toBeTruthy()
  })
})

describe('the create dialog', () => {
  const openDialog = async () => {
    await act(async () => {
      screen.getByText('+ gui.kb.new').click()
    })
  }

  it('takes a name and an embedding model, and creates with them', async () => {
    const made: string[] = []
    source({
      status: async () => ({ configured: true, model: 'bge-m3' }),
      create: async (name: string) => {
        made.push(name)
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

    expect(made).toEqual(['handbook'])
    expect(document.querySelector('.kbdlg')).toBeNull()
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

  it('sorts every offered format into framed, converted or neither', () => {
    const kind = (name: string) => store.previewKind(doc({ id: 'x', source: name }))
    expect(kind('a.pdf')).toBe('native')
    expect(kind('a.png')).toBe('native')
    expect(kind('a.md')).toBe('native')
    expect(kind('a.docx')).toBe('converted')
    expect(kind('a.XLS')).toBe('converted')
    expect(kind('a.zip')).toBe('none')
    /* No suffix at all is not a format anyone can guess at. */
    expect(kind('README')).toBe('none')
  })
})
