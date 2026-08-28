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

  it('says which document a hit came from', async () => {
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [
        doc({ id: 'd1', source: 'handbook.md', status: 'ready' }),
        doc({ id: 'd2', source: 'policy.md', status: 'ready' }),
      ],
      search: async () => [{ score: 0.7, document_id: 'd2', text: 'the answer' }],
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await act(async () => {
      await store.searchNow('what')
    })
    /* The score alone cannot answer "found where?" once a base holds more than
       one document, and the id rides on every hit already. */
    expect(screen.getByText('gui.kb.from_doc {"name":"policy.md"}')).toBeTruthy()
  })

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
    expect(screen.getByText('the newest answer')).toBeTruthy()
    expect(screen.queryByText('the stale answer')).toBeNull()
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
    expect(screen.getByText('the answer')).toBeTruthy()
    await act(async () => {
      store.search('')
    })
    expect(screen.queryByText('the answer')).toBeNull()
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
    /* Driven through the DOM, not the store: what is being pinned is the box
       being wired to `searchNow`, and a test that calls the store directly
       passes with the handler deleted. */
    const box = document.querySelector('input.kbask') as HTMLInputElement
    await act(async () => {
      fireEvent.change(box, { target: { value: 'quarterly' } })
    })
    expect(calls).toBe(0)
    await act(async () => {
      fireEvent.keyDown(box, { key: 'Enter' })
    })
    expect(screen.getByText('straight away')).toBeTruthy()
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
    expect(screen.getByText('gui.kb.chunks {"n":3}')).toBeTruthy()
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
    expect(screen.getByText('two')).toBeTruthy()
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
    expect(screen.getByText('gui.kb.doc_ready')).toBeTruthy()
    expect(screen.getByText('gui.kb.chunks {"n":2}')).toBeTruthy()
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
    expect(screen.getByText('the answer')).toBeTruthy()
    /* Two decimals: a similarity is for ranking by eye. */
    expect(screen.getByText('0.81')).toBeTruthy()
    await act(async () => {
      await store.searchNow('  ')
    })
    expect(screen.queryByText('the answer')).toBeNull()
    expect(screen.getByText('onboarding.md')).toBeTruthy()
  })

  it('offers a way out only on a row that is stuck', async () => {
    /* Two buttons on every row would bury the one row that needs them, and a
       `ready` document has nowhere to go. */
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [
        doc({ id: 'd1', source: 'done.md', status: 'ready', chunk_count: 2 }),
        doc({ id: 'd2', source: 'stuck.md', status: 'pending' }),
        doc({ id: 'd3', source: 'broke.md', status: 'failed', error: 'endpoint said 400' }),
      ],
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    expect(screen.getAllByText('gui.kb.doc_retry').length).toBe(2)
    expect(screen.getAllByText('gui.kb.doc_delete').length).toBe(2)
    expect(screen.getByText('done.md').closest('.kbdoc')!.querySelector('button')).toBeNull()
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
    await act(async () => {
      screen.getByText('gui.kb.doc_retry').click()
    })
    /* The same call upload makes: `index_document` re-embeds from the stored
       blob, so an endpoint failure clears with nothing else to do. */
    expect(asked).toEqual(['d2'])
    expect(screen.getByText('gui.kb.doc_ready')).toBeTruthy()
    expect(screen.queryByText('gui.kb.doc_retry')).toBeNull()
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
    await act(async () => {
      screen.getByText('gui.kb.doc_retry').click()
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
    await act(async () => {
      screen.getByText('gui.kb.doc_retry').click()
    })
    const remove = screen.getByText('gui.kb.doc_delete').closest('button') as HTMLButtonElement
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
    await act(async () => {
      screen.getByText('gui.kb.doc_delete').click()
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
    expect(screen.getByText('gui.kb.no_hits')).toBeTruthy()
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
