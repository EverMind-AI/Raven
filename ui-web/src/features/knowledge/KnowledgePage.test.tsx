// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { KnowledgeApp } from './KnowledgePage'
import * as store from './store'

import type { KbBase, KbChunk, KbDoc, KbSearch, KnowledgeSource } from './types'
import type { Shell } from '../../shell/bridge'

/* pdf.js, stubbed at the seam that loads it. The real one is not bundled -- it
   is two files copied beside the page and imported at run time -- so there is
   no module specifier here to intercept, which is the point of the loader
   having its own module. What these tests are about is the rest: which pages
   are laid out, where a clicked piece is marked, and where the column is
   scrolled to, none of which needs a renderer, and happy-dom has no canvas to
   give one. */
const PDF_PAGE_WIDTH = 600
const PDF_PAGE_HEIGHT = 800

/* What the viewer last asked pdf.js to open. The url is how a test says which
   document is being shown and whether the gateway was asked to convert it --
   the question the iframe's `src` used to answer. */
const pdfAsked = vi.hoisted(() => ({ url: '', broken: false }))

vi.mock('./pdfjs', () => ({
  loadPdfjs: async () => {
    if (pdfAsked.broken) throw new Error('pdf.js was not served')
    return {
      GlobalWorkerOptions: { workerSrc: '' },
      getDocument: ({ url }: { url: string }) => {
        pdfAsked.url = url
        return {
          promise: Promise.resolve({
            numPages: 8,
            getPage: async () => ({
              getViewport: ({ scale }: { scale: number }) => ({
                width: PDF_PAGE_WIDTH * scale,
                height: PDF_PAGE_HEIGHT * scale
              }),
              render: () => ({ promise: Promise.resolve() })
            })
          })
        }
      }
    }
  }
}))

/* Where the viewer asked to be scrolled. happy-dom has no layout, so a
   scroller's `scrollTop` never moves however it is told to; what a test can
   see is the ask. */
function recordScrolls(): { tops: number[]; restore: () => void } {
  const tops: number[] = []
  const before = HTMLElement.prototype.scrollTo
  HTMLElement.prototype.scrollTo = function (options?: unknown): void {
    tops.push(Math.round((options as { top?: number } | undefined)?.top ?? 0))
  } as typeof HTMLElement.prototype.scrollTo
  return { tops, restore: () => (HTMLElement.prototype.scrollTo = before) }
}

function base(over: Partial<KbBase> & { id: string }): KbBase {
  return {
    name: 'handbook',
    description: '',
    embedding_model: 'bge-m3',
    dimensions: 1024,
    created_at: '2026-08-24T00:00:00',
    updated_at: '2026-08-24T00:00:00',
    documents: 0,
    ...over
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
const toasts = (): string[] => Array.from(toastHost().querySelectorAll('.toast .t')).map(e => e.textContent || '')
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
    ...over
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
    closeDetail: () => {}
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
      chunks: async () => ({ chunks: [], total: 0 }),
      embeddingModels: async () => [
        { id: 'siliconflow', name: 'SiliconFlow', models: ['BAAI/bge-large-zh-v1.5', 'BAAI/bge-m3'] },
        { id: 'dashscope', name: 'DashScope', models: ['text-embedding-v4'] }
      ],
      switchChunks: async () => 0,
      deleteChunks: async () => 0,
      createChunk: async (_d: string, text: string) => ({ chunk_index: 9, total_chunks: 10, text, manual: true }),
      updateChunk: async (_d: string, _c: string, text: string) => ({
        chunk_index: 0,
        total_chunks: 1,
        text,
        manual: true
      }),
      removeDoc: async () => {},
      ...over
    }
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
  const at = names.findIndex(c => c.textContent === name)
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
      search: () => new Promise(r => gates.push(r as never))
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
    const texts = (store.getState().hits ?? []).map(h => h.text)
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
      }
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
    expect((store.getState().hits ?? []).map(h => h.text)).toEqual(['the answer'])

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
      bases: async () => [base({ id: 'b1', embedding_model: 'bge-m3' })]
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
      }
    })
    await mount()
    expect(screen.getByText('socket dropped')).toBeTruthy()
    expect(screen.queryByText('gui.kb.none')).toBeNull()
  })

  it('claims nothing before the first answer lands', async () => {
    /* Without this the first paint says "no bases" while the call is still in
       flight. */
    let release: (v: KbBase[]) => void = () => {}
    source({ bases: () => new Promise<KbBase[]>(r => (release = r)) })
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
      }
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
      bases: async () => listed
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
      }
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
          data: { detail: 'could not create the base: embedding endpoint returned 402' }
        }
      }
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
        return new Promise<KbBase>(r => (release = r))
      }
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
      }
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
          updated_at: ''
        }
      ]
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    expect(screen.getByText('onboarding.md')).toBeTruthy()
    expect(screen.getByText('gui.kb.doc_ready')).toBeTruthy()
    expect(screen.getByText('gui.kb.col_updated')).toBeTruthy()
    /* The count is in the table now rather than only behind View Chunks: how
       many pieces a file became is the first thing a reader checks when a
       search answers with less than they expected, and opening every file to
       find out is a poor way to compare them. */
    expect(screen.getByText('gui.kb.col_chunks')).toBeTruthy()
    const cells = [...document.querySelectorAll('.kbtable .td')].map(c => c.textContent)
    expect(cells).toContain('3')
  })

  it('shows a dash rather than a zero for a file with no chunks', async () => {
    /* A document that failed, one still queued and one in a base with no model
       have no count to report, and a zero would read as a file that was cut
       into nothing. */
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [
        doc({ id: 'd1', source: 'broken.pdf', status: 'failed', chunk_count: 0 }),
        doc({ id: 'd2', source: 'queued.md', status: 'pending', chunk_count: 0 })
      ]
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    const cells = [...document.querySelectorAll('.kbtable .td')].map(c => c.textContent)
    expect(cells.filter(text => text === '\u2014').length).toBe(2)
    expect(cells).not.toContain('0')
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
          updated_at: ''
        }
      ]
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

  it('marks a searchable file that part of itself did not make it into', async () => {
    /* The case the mark exists for: the row says ready, the file is searchable,
       and a search about the pictures in it answers worse for a reason nothing
       on the page would otherwise state. */
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [
        doc({
          id: 'd1',
          source: 'report.docx',
          status: 'ready',
          chunk_count: 7,
          warning: '2 of 5 pictures in this file could not be read: rate limited'
        })
      ]
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    const status = document.querySelector('.kbtable .td.s-ready') as HTMLElement
    expect(status.getAttribute('title')).toContain('2 of 5 pictures')
    expect(status.querySelector('.kbdocwarn')).not.toBeNull()
    /* Still ready, and still counted: a warning is not a failure. */
    expect(status.textContent).toContain('gui.kb.doc_ready')
  })

  it('leaves an unwarned row unmarked', async () => {
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd1', status: 'ready', chunk_count: 7 })]
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    expect(document.querySelector('.kbdocwarn')).toBeNull()
    expect((document.querySelector('.kbtable .td.s-ready') as HTMLElement).getAttribute('title')).toBeNull()
  })

  it('shows the failure rather than the warning when a row has both', async () => {
    /* It cannot in practice -- a document that failed has no parse to warn
       about -- but the cell has to pick one, and the reason it did not index is
       the more urgent of the two. */
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [
        doc({ id: 'd1', status: 'failed', error: 'embedding endpoint returned 404', warning: 'a picture' })
      ]
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    const status = document.querySelector('.kbtable .td.s-failed') as HTMLElement
    expect(status.getAttribute('title')).toBe('embedding endpoint returned 404')
    expect(status.querySelector('.kbdocwarn')).toBeNull()
  })

  it('says when a search answered by words instead of by meaning', async () => {
    /* The failure this exists for: an unreachable embedding endpoint turns a
       search into a perfectly ordinary looking set of hits, scored on another
       scale, with nothing anywhere saying retrieval changed mode. */
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd1', status: 'ready' })],
      search: async () => ({
        hits: [{ score: 8.42, retrieval: 'keyword' as const, document_id: 'd1', text: 'alpha' }],
        by_keyword: [{ base_id: 'b1', reason: "the model 'bge-m3' could not be reached" }],
        search_ms: 3,
        embed_ms: 0
      })
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await act(async () => {
      store.openRecall()
    })
    await act(async () => {
      fireEvent.change(document.querySelector('.kbask .kbname') as HTMLInputElement, {
        target: { value: 'alpha' }
      })
    })
    await act(async () => {
      screen.getByText('gui.kb.recall_run').click()
    })

    const notice = document.querySelector('.kbfellback') as HTMLElement
    expect(notice).not.toBeNull()
    expect(notice.textContent).toContain('could not be reached')
    /* And the score is not dressed as a similarity. */
    const score = document.querySelector('.kbhitsc') as HTMLElement
    expect(score.classList.contains('kbhitbm')).toBe(true)
    expect(score.textContent).toContain('gui.kb.recall_kw')
    expect(score.getAttribute('title')).toBe('gui.kb.recall_bm25')
  })

  it('leaves an ordinary search unmarked', async () => {
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd1', status: 'ready' })],
      search: async () => ({
        hits: [{ score: 0.83, document_id: 'd1', text: 'alpha' }],
        by_keyword: [],
        search_ms: 3,
        embed_ms: 12
      })
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await act(async () => {
      store.openRecall()
    })
    await act(async () => {
      fireEvent.change(document.querySelector('.kbask .kbname') as HTMLInputElement, {
        target: { value: 'alpha' }
      })
    })
    await act(async () => {
      screen.getByText('gui.kb.recall_run').click()
    })

    expect(document.querySelector('.kbfellback')).toBeNull()
    const score = document.querySelector('.kbhitsc') as HTMLElement
    expect(score.classList.contains('kbhitbm')).toBe(false)
    expect(score.getAttribute('title')).toBe('gui.kb.recall_cosine')
  })

  it('does not answer into a panel the reader has left', async () => {
    /* Opening b1 then b2 must not paint b1 documents under b2. */
    let release: (d: never[]) => void = () => {}
    source({
      bases: async () => [base({ id: 'b1', name: 'one' }), base({ id: 'b2', name: 'two' })],
      documents: (id: string) => (id === 'b1' ? new Promise(r => (release = r as never)) : Promise.resolve([]))
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
      }
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
      }
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
        doc({ id: 'd3', source: 'broke.md', status: 'failed', error: 'endpoint said 400' })
      ]
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    expect(document.querySelectorAll('.kbtable .kbops .dots').length).toBe(2)
    await openRowMenu('done.md')
    expect(screen.getByText('gui.kb.doc_reindex')).toBeTruthy()
    expect(screen.getByText('gui.kb.delete')).toBeTruthy()
    expect((document.querySelector('.kbtable .td.s-failed') as HTMLElement).getAttribute('title')).toBe(
      'endpoint said 400'
    )
  })

  it('indexes a stuck row again, and shows the answer', async () => {
    const asked: string[] = []
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd2', source: 'stuck.md', status: 'pending' })],
      index: async (id: string) => {
        asked.push(id)
        return doc({ id, source: 'stuck.md', status: 'ready', chunk_count: 4 })
      }
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
      }
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
    expect(toasts().some(m => m.includes('still 400'))).toBe(true)
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
      index: () => new Promise<KbDoc>(r => (release = r)),
      removeDoc: async (id: string) => {
        gone.push(id)
      }
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
      }
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
    expect(confirms.some(c => c.includes('gui.kb.doc_delete_body'))).toBe(true)
    expect(gone).toEqual(['d2'])
    expect(screen.queryByText('stuck.md')).toBeNull()
  })

  it('tells an empty result apart from not having asked', async () => {
    source({
      bases: async () => [base({ id: 'b1' })],
      search: async () => ({ hits: [], search_ms: 4, embed_ms: 120 })
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
      search: () => new Promise(r => (release = r as never))
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
      documents: async () => [doc({ id: 'd1', source: 'onboarding.md', status: 'ready' })]
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    const rail = [...document.querySelectorAll('.kbrail .kbrow .nm')].map(n => n.textContent)
    expect(rail).toEqual(['handbook', 'policies'])
    expect(document.querySelector('.kbhd b')!.textContent).toBe('handbook')
    /* And the open one is marked, since the rail is what says which base the
       panel belongs to. */
    const on = [...document.querySelectorAll('.kbrail .kbrow')].filter(r => r.hasAttribute('aria-current'))
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
    /* Disable has nothing behind it. It is on screen because the design puts
       it there, and disabled because a control that looks live and does
       nothing when pressed is worse than one that says it is not ready. */
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd1', source: 'onboarding.md', status: 'ready' })]
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    await openRowMenu('onboarding.md')
    for (const label of ['gui.kb.doc_disable']) {
      expect((screen.getByText(label).closest('button') as HTMLButtonElement).disabled).toBe(true)
    }
    /* View Chunks is wired now: it opens the same panel clicking the name
       does. */
    expect((screen.getByText('gui.kb.doc_view_chunks').closest('button') as HTMLButtonElement).disabled).toBe(false)
    /* Every data source in the menu is wired, so the button that opens it is
       not one of the stubs. */
    const add = screen.getByText('+ gui.kb.add_source').closest('button') as HTMLButtonElement
    expect(add.disabled).toBe(false)
    /* Recall Test is wired now too, so it is no longer a stub. */
    const recall = screen.getByText('gui.kb.recall_test').closest('button') as HTMLButtonElement
    expect(recall.disabled).toBe(false)
    expect(recall.title).toBe('')
    /* Settings is a glyph, so its name is on the control rather than in it --
       the title is what says which button this is, not that it is unbuilt. */
    const gear = screen.getByLabelText('gui.kb.settings') as HTMLButtonElement
    expect(gear.disabled).toBe(false)
    expect(gear.querySelector('svg')).not.toBeNull()
    expect(gear.textContent).toBe('')
  })
})

describe('the create dialog', () => {
  const openDialog = async () => {
    await act(async () => {
      screen.getByText('+ gui.kb.new').click()
    })
  }

  it('takes a name and an embedding model, and creates with them', async () => {
    const made: Array<[string, boolean | undefined, string | undefined, string | undefined]> = []
    source({
      status: async () => ({ configured: true, model: 'BAAI/bge-m3', provider: 'siliconflow' }),
      create: async (name: string, _d: string, embedding?: boolean, model?: string, provider?: string) => {
        made.push([name, embedding, model, provider])
        return base({ id: 'b1', name })
      }
    })
    await mount()
    await openDialog()

    /* Every model the install can reach, grouped by who serves it, with the
       configured default preselected -- and Disabled as a real choice, not the
       absence of one. */
    const picker = document.getElementById('kbembed') as HTMLSelectElement
    expect(picker.value).toBe('siliconflow::BAAI/bge-m3')
    expect([...picker.options].map(o => o.value)).toEqual([
      '',
      'siliconflow::BAAI/bge-large-zh-v1.5',
      'siliconflow::BAAI/bge-m3',
      'dashscope::text-embedding-v4'
    ])

    const field = document.getElementById('kbname') as HTMLInputElement
    await act(async () => {
      fireEvent.change(field, { target: { value: 'handbook' } })
    })
    await act(async () => {
      fireEvent.change(picker, { target: { value: 'dashscope::text-embedding-v4' } })
    })
    await act(async () => {
      screen.getByText('gui.kb.create').click()
    })

    expect(made).toEqual([['handbook', true, 'text-embedding-v4', 'dashscope']])
    expect(document.querySelector('.kbdlg')).toBeNull()
  })

  it('offers the configured model even when it is not in any provider list', async () => {
    /* A select whose value matches no option draws as empty, which would tell
       a reader the base they are about to make has no model. */
    source({ status: async () => ({ configured: true, model: 'house-embed', provider: 'custom' }) })
    await mount()
    await openDialog()

    const picker = document.getElementById('kbembed') as HTMLSelectElement
    expect(picker.value).toBe('custom::house-embed')
    expect([...picker.options].map(o => o.textContent)).toContain('house-embed')
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
      }
    })
    await mount()
    await openDialog()

    const field = document.getElementById('kbname') as HTMLInputElement
    await act(async () => {
      fireEvent.change(field, { target: { value: 't3' } })
    })
    // The configured pair is the option the select opens on.
    expect((document.getElementById('kbembed') as HTMLSelectElement).value).toBe('::bge-m3')
    await act(async () => {
      fireEvent.change(document.getElementById('kbembed') as HTMLSelectElement, { target: { value: '' } })
    })
    await act(async () => {
      screen.getByText('gui.kb.create').click()
    })

    expect(made).toEqual([['t3', false]])
  })

  it('sends no model at all when Disabled is picked', async () => {
    /* Not the model beside an `embedding: false`: a base with no vectors has
       no model, and sending one would be describing an index it does not
       have. */
    const made: Array<[boolean | undefined, string | undefined]> = []
    source({
      create: async (_n: string, _d: string, embedding?: boolean, model?: string) => {
        made.push([embedding, model])
        return base({ id: 'b1', name: 't3', embedding_model: '' })
      }
    })
    await mount()
    await openDialog()

    await act(async () => {
      fireEvent.change(document.getElementById('kbname') as HTMLInputElement, { target: { value: 't3' } })
    })
    await act(async () => {
      fireEvent.change(document.getElementById('kbembed') as HTMLSelectElement, { target: { value: '' } })
    })
    await act(async () => {
      screen.getByText('gui.kb.create').click()
    })

    expect(made).toEqual([[false, '']])
  })

  it('says Disabled on a base that has no model of its own', async () => {
    source({
      status: async () => ({ configured: true, model: 'bge-m3' }),
      bases: async () => [base({ id: 'b1', name: 't3', embedding_model: '' })]
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
      }
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

  it('draws the file when its name is clicked, and goes back', async () => {
    /* A PDF is drawn here rather than framed, which is what lets a piece be
       marked on the page it came from: a framed viewer is sandboxed to an
       opaque origin and can only be told a page number. */
    await openBase([doc({ id: 'd1', source: 'contract.pdf', status: 'ready' })])

    await clickName('contract.pdf')
    await act(async () => {})

    expect(document.querySelector('.kbpdf')).not.toBeNull()
    expect(pdfAsked.url).toBe('/knowledge/file?document=d1')
    expect(document.querySelector('.kbtable')).toBeNull()

    await act(async () => {
      ;(document.querySelector('.kbback') as HTMLButtonElement).click()
    })
    expect(document.querySelector('.kbpdf')).toBeNull()
    expect(document.querySelector('.kbtable')).not.toBeNull()
  })

  it('frames the document when pdf.js is not there to draw it', async () => {
    /* The library is copied beside the page at build time rather than bundled,
       so a build that skipped it -- no node_modules, an older wheel -- has to
       degrade to what this did before rather than to an empty panel. */
    pdfAsked.broken = true
    try {
      await openBase([doc({ id: 'd1', source: 'contract.pdf', status: 'ready' })])
      await clickName('contract.pdf')
      await act(async () => {})

      const frame = document.querySelector('.kbframe') as HTMLIFrameElement
      expect(frame).not.toBeNull()
      expect(frame.getAttribute('src')).toBe('/knowledge/file?document=d1')

      /* And a clicked piece still moves it, by the one control a sandboxed
         viewer has. Losing that as well would make a build without pdf.js
         worse than the one this replaced. */
      await act(async () => {
        store.focusChunk({ chunk_index: 0, total_chunks: 1, text: 'x', chunk_id: 'c1', page_number: 5 })
      })
      expect((document.querySelector('.kbframe') as HTMLIFrameElement).getAttribute('src')).toBe(
        '/knowledge/file?document=d1#page=5',
      )
    } finally {
      pdfAsked.broken = false
    }
  })

  it('frames the kinds that have no regions to point at, without a sandbox attribute', async () => {
    /* The response already carries a CSP sandbox; the attribute as well would
       stop the browser drawing anything at all. */
    await openBase([doc({ id: 'd1', source: 'diagram.png', status: 'ready' })])

    await clickName('diagram.png')

    const frame = document.querySelector('.kbframe') as HTMLIFrameElement
    expect(frame.getAttribute('src')).toBe('/knowledge/file?document=d1')
    expect(frame.hasAttribute('sandbox')).toBe(false)
  })

  it('takes the whole page while a file is open, rail and all', async () => {
    /* The widest thing on the screen should be the thing being read. Picking
       another base is not a move anyone makes mid-document, and the way back
       is the one button in the header. */
    await openBase([doc({ id: 'd1', source: 'contract.pdf', status: 'ready' })])

    await clickName('contract.pdf')

    expect(document.querySelector('.kbview')).not.toBeNull()
    expect(document.querySelector('.kbsplit')).toBeNull()
    expect(document.querySelector('.kbrail')).toBeNull()

    await act(async () => {
      ;(document.querySelector('.kbback') as HTMLButtonElement).click()
    })
    expect(document.querySelector('.kbrail')).not.toBeNull()
    expect(document.querySelector('.kbview')).toBeNull()
  })

  it('shows the chunks beside the file, in reading order', async () => {
    /* The two halves answer one question: what the file says, and what the
       index actually holds of it. Reading order is the chunker's numbering,
       so the list runs down the document. */
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook' })],
      documents: async () => [doc({ id: 'd1', source: 'contract.pdf', status: 'ready' })],
      chunks: async () => ({
        chunks: [
          {
            chunk_index: 1,
            total_chunks: 2,
            text: 'Second piece.',
            layout_type: 'text',
            page_number: 2,
            chunk_id: 'c2'
          },
          {
            chunk_index: 0,
            total_chunks: 2,
            text: 'First piece.',
            layout_type: 'heading',
            page_number: 1,
            heading_path: ['Terms'],
            chunk_id: 'c1'
          }
        ],
        total: 2
      })
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    await clickName('contract.pdf')
    await act(async () => {})

    expect(document.querySelector('.kborig .kbpdf')).not.toBeNull()
    const rows = [...document.querySelectorAll('.kbchunk .kbchunktx')].map(n => n.textContent)
    /* Answered out of order on purpose: the panel shows the document's order,
       not the transport's. */
    expect(rows).toEqual(['First piece.', 'Second piece.'])
    const first = document.querySelector('.kbchunk') as HTMLElement
    expect(first.querySelector('.kbchunkix')?.textContent).toBe('#1')
    expect(first.querySelector('.kbchunkty')?.textContent).toBe('heading')
    expect(first.querySelector('.kbchunkpath')?.textContent).toBe('Terms')
  })

  it('says on the row when a piece was cut from more than one section', async () => {
    /* The row's page and heading are the first part's, which is all they can
       be once a chunk can merge across sections. Shown alone they read as
       facts about the whole piece: this one is half of section 7 on page 9,
       labelled as section 3 on page 4, with nothing saying so. */
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook' })],
      documents: async () => [doc({ id: 'd1', source: 'contract.pdf', status: 'ready' })],
      chunks: async () => ({
        chunks: [
          {
            chunk_index: 0,
            total_chunks: 1,
            text: 'Third.\nSeventh.',
            layout_type: 'text',
            page_number: 4,
            page_end: 9,
            heading_path: ['Handbook', 'Part 3'],
            parts: [
              {
                char_start: 0,
                char_end: 6,
                page_number: 4,
                heading_path: ['Handbook', 'Part 3'],
                section_ordinal: 3
              },
              {
                char_start: 7,
                char_end: 15,
                page_number: 9,
                heading_path: ['Handbook', 'Part 7'],
                section_ordinal: 7
              }
            ],
            chunk_id: 'c1'
          }
        ],
        total: 1
      })
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await clickName('contract.pdf')
    await act(async () => {})

    const row = document.querySelector('.kbchunk') as HTMLElement
    /* The page badge stops claiming one page. */
    expect(row.querySelector('.kbchunkpg')?.textContent).toBe('gui.kb.chunks_pages {"from":4,"to":9}')
    /* And the badge names the sections, with every part behind the hover --
       the only route on this row to where the second half came from. */
    const span = row.querySelector('.kbchunkspan') as HTMLElement
    expect(span.textContent).toBe('gui.kb.chunk_sections {"n":2}')
    expect(span.getAttribute('title')).toContain('"section":4')
    expect(span.getAttribute('title')).toContain('"section":8')
    expect(span.getAttribute('title')).toContain('Handbook > Part 7')
  })

  it('leaves a piece that merged nothing exactly as it was', async () => {
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook' })],
      documents: async () => [doc({ id: 'd1', source: 'contract.pdf', status: 'ready' })],
      chunks: async () => ({
        chunks: [
          {
            chunk_index: 0,
            total_chunks: 1,
            text: 'Alone.',
            page_number: 4,
            heading_path: ['Handbook'],
            chunk_id: 'c1'
          }
        ],
        total: 1
      })
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await clickName('contract.pdf')
    await act(async () => {})

    const row = document.querySelector('.kbchunk') as HTMLElement
    expect(row.querySelector('.kbchunkspan')).toBeNull()
    expect(row.querySelector('.kbchunkpg')?.textContent).toBe('gui.kb.chunks_page {"n":4}')
  })

  const chunk = (over: Partial<KbChunk> = {}): KbChunk => ({
    chunk_index: 0,
    total_chunks: 1,
    text: 'a piece of it',
    chunk_id: 'c1',
    enabled: true,
    manual: false,
    ...over
  })

  const openWithChunks = async (chunks: KbChunk[], total = chunks.length, over = {}) => {
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd1', source: 'contract.pdf', status: 'ready' })],
      chunks: async () => ({ chunks, total }),
      ...over
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await clickName('contract.pdf')
    await act(async () => {})
  }

  it('draws a table a piece carries as a table', async () => {
    /* A PDF's and a Word file's tables are indexed as HTML, because a grid
       flattened to lines loses the column a cell stood under. Shown as markup
       it would be worse to read than the lines were. */
    const html = '<table>\n<tr><th>Region</th><th>Q1</th></tr>\n<tr><td>EU</td><td>1.2M</td></tr>\n</table>'
    await openWithChunks([chunk({ chunk_id: 'c1', text: `Before it.\n${html}\nAfter it.` })])

    const table = document.querySelector('.kbchunktbl table') as HTMLTableElement
    expect(table.querySelectorAll('th')).toHaveLength(2)
    expect(table.querySelectorAll('tr')).toHaveLength(2)
    expect(table.textContent).toContain('1.2M')
    /* The prose either side is the chunker's lifted context, and it is text. */
    expect((document.querySelector('.kbchunktx') as HTMLElement).textContent).toContain('Before it.')
  })

  it('carries a cell that spans columns across as one cell', async () => {
    const html = '<table>\n<tr><th colspan="2">Revenue</th></tr>\n<tr><td>EU</td><td>US</td></tr>\n</table>'
    await openWithChunks([chunk({ chunk_id: 'c1', text: html })])

    const head = document.querySelector('.kbchunktbl th') as HTMLTableCellElement
    expect(head.colSpan).toBe(2)
  })

  it('never lets a piece of text become markup', async () => {
    /* A person can rewrite any piece from this panel, so what comes back from
       the store is user input however it got there. Nothing here is injected:
       the table is parsed and rebuilt, and only the text of each cell lives. */
    const html = '<table><tr><td><img src=x onerror="window.__x=1"></td></tr><tr><td>b</td></tr></table>'
    await openWithChunks([chunk({ chunk_id: 'c1', text: html })])

    expect(document.querySelector('.kbchunktbl img')).toBeNull()
    expect((window as unknown as { __x?: number }).__x).toBeUndefined()
  })

  it('shows text that only looks like a table as the text it is', async () => {
    await openWithChunks([chunk({ chunk_id: 'c1', text: '<table>not really</table>' })])

    expect(document.querySelector('.kbchunktbl')).toBeNull()
    expect((document.querySelector('.kbchunktx') as HTMLElement).textContent).toContain('not really')
  })

  it('marks a clicked piece on the page it was cut from, and scrolls to it', async () => {
    /* The whole point of drawing the document rather than framing it. A chunk
       is not a page -- it is a paragraph or a table on one -- and the question
       a reader has when a retrieved passage looks wrong is which part of the
       page it came from. */
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd1', source: 'report.pdf', status: 'ready' })],
      chunks: async () => ({
        chunks: [
          {
            chunk_index: 0,
            total_chunks: 1,
            text: 'On page three.',
            chunk_id: 'c1',
            page_number: 3,
            regions: [{ page_number: 3, x0: 60, top: 100, x1: 400, bottom: 220 }]
          }
        ],
        total: 1
      })
    })
    const scrolled = recordScrolls()
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await clickName('report.pdf')
    await act(async () => {})

    await act(async () => {
      ;(document.querySelector('.kbchunkpg') as HTMLButtonElement).click()
    })

    const mark = document.querySelector('.kbpdfmark') as HTMLElement
    expect(mark).not.toBeNull()
    /* Points to pixels at the fitted scale, which is 1 where the panel has no
       measurable width -- so the numbers are the region's own. */
    expect(mark.style.left).toBe('60px')
    expect(mark.style.top).toBe('100px')
    expect(mark.style.width).toBe('340px')
    expect(mark.style.height).toBe('120px')

    /* Two pages of 800 and the 8px between them sit above page three, and the
       region is parked below the top of the scroller rather than against it. */
    expect(scrolled.tops.at(-1)).toBe(2 * (PDF_PAGE_HEIGHT + 8) + 100 - 64)
    scrolled.restore()
  })

  it('marks every region of a piece that crossed a page boundary', async () => {
    /* The flattened fields say where a merged chunk *starts*. Marking only
       that one would say the piece stopped at the page break. */
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd1', source: 'report.pdf', status: 'ready' })],
      chunks: async () => ({
        chunks: [
          {
            chunk_index: 0,
            total_chunks: 1,
            text: 'Across the break.',
            chunk_id: 'c1',
            page_number: 1,
            regions: [
              { page_number: 1, x0: 60, top: 600, x1: 400, bottom: 740 },
              { page_number: 2, x0: 60, top: 80, x1: 400, bottom: 160 }
            ]
          }
        ],
        total: 1
      })
    })
    const scrolled = recordScrolls()
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await clickName('report.pdf')
    await act(async () => {})

    await act(async () => {
      ;(document.querySelector('.kbchunkpg') as HTMLButtonElement).click()
    })

    expect(document.querySelectorAll('.kbpdfmark')).toHaveLength(2)
    /* Scrolled to the first of them, which is the one on page one. */
    expect(scrolled.tops.at(-1)).toBe(600 - 64)
    scrolled.restore()
  })

  it('shows the region a piece was cut from, addressed by both ids', async () => {
    /* The text is what the search matches; this is the only thing on the page
       that can answer whether the parser read the region correctly. */
    await openWithChunks([chunk({ chunk_id: 'c1', has_crop: true, page_number: 4 })])

    const crop = document.querySelector('.kbchunkcrop') as HTMLImageElement
    expect(crop.getAttribute('src')).toBe('/knowledge/crop?document=d1&chunk=c1')
    /* Lazily, because a page of twenty would otherwise pull twenty images
       before the reader has scrolled to the second one. */
    expect(crop.getAttribute('loading')).toBe('lazy')
  })

  it('asks for no picture where there is none', async () => {
    /* A format with no pages, a piece a person wrote, a parser that knew the
       page but not the position. A broken image is worse than no image. */
    await openWithChunks([chunk({ chunk_id: 'c1', has_crop: false })])

    expect(document.querySelector('.kbchunkcrop')).toBeNull()
  })

  it('keeps the picture in the cut-down view, shorter', async () => {
    /* The panel opens in the cut-down view, so leaving the picture out of it
       would hide the feature behind a control nobody has a reason to press.
       It is shortened instead, so a row stays a row. */
    await openWithChunks([chunk({ chunk_id: 'c1', has_crop: true })])
    await act(async () => {
      store.setChunkView('ellipse')
    })

    expect(document.querySelector('.kbchunkcrop')?.className).toContain('small')

    await act(async () => {
      store.setChunkView('full')
    })
    expect(document.querySelector('.kbchunkcrop')?.className).not.toContain('small')
  })

  it('turns one chunk off from its own switch', async () => {
    /* Disabled is not a ranking penalty: the engine drops it from retrieval
       entirely, so the switch is worth showing on the row itself. */
    const asked: unknown[] = []
    await openWithChunks([chunk({ chunk_id: 'c1' })], 1, {
      switchChunks: async (d: string, ids: string[], on: boolean) => {
        asked.push([d, ids, on])
        return 1
      }
    })

    const box = document.querySelector('.kbswitch input') as HTMLInputElement
    expect(box.checked).toBe(true)
    await act(async () => {
      box.click()
    })

    expect(asked).toEqual([['d1', ['c1'], false]])
  })

  it('offers the three operations only once something is ticked', async () => {
    /* Absent rather than dead: three buttons that can never be pressed are
       three things to read past, in a toolbar that is already full. */
    const asked: unknown[] = []
    await openWithChunks([chunk({ chunk_id: 'c1' }), chunk({ chunk_index: 1, chunk_id: 'c2' })], 2, {
      switchChunks: async (_d: string, ids: string[], on: boolean) => {
        asked.push([ids, on])
        return ids.length
      }
    })

    expect(screen.queryByText('gui.kb.chunk_disable')).toBeNull()
    expect(screen.queryByText('gui.kb.chunk_delete')).toBeNull()

    await act(async () => {
      ;(document.querySelector('.kbpickall input') as HTMLInputElement).click()
    })

    expect(screen.getByText('gui.kb.picked_n {"n":2}')).toBeTruthy()
    await act(async () => {
      ;(screen.getByText('gui.kb.chunk_disable').closest('button') as HTMLButtonElement).click()
    })

    expect(asked).toEqual([[['c1', 'c2'], false]])
  })

  it('pages the reading order twenty at a time', async () => {
    const asked: unknown[] = []
    await openWithChunks([chunk()], 45, {
      chunks: async (_d: string, opts: { page?: number; page_size?: number }) => {
        asked.push(opts)
        return { chunks: [chunk()], total: 45 }
      }
    })

    expect(asked[0]).toMatchObject({ page: 1, page_size: 20 })
    /* 45 pieces is three pages, so the pager is there and the first page has
       nowhere back to go. */
    expect(screen.getByText('gui.kb.chunk_page_of {"page":1,"pages":3}')).toBeTruthy()
    await act(async () => {
      ;(document.querySelector('[aria-label="gui.kb.chunk_next"]') as HTMLButtonElement).click()
    })
    expect(asked[asked.length - 1]).toMatchObject({ page: 2 })
  })

  it('searches on Enter without waiting, and not on a keystroke', async () => {
    /* A search embeds the query at whatever endpoint the base was built with,
       so typing does not spend one. The box runs the same retrieval a search
       of the base does, narrowed to this file -- a word that is not on this
       page still finds its chunk. */
    const asked: unknown[] = []
    await openWithChunks([chunk()], 40, {
      chunks: async (_d: string, opts: { query?: string }) => {
        asked.push(opts)
        return { chunks: [chunk({ text: 'the matching piece' })], total: 1 }
      }
    })
    const before = asked.length

    const box = document.querySelector('.kbchunksearch') as HTMLInputElement
    await act(async () => {
      fireEvent.change(box, { target: { value: 'latency' } })
    })
    expect(asked.length).toBe(before)
    expect(box.value).toBe('latency')

    await act(async () => {
      fireEvent.keyDown(box, { key: 'Enter' })
    })

    expect(asked[asked.length - 1]).toMatchObject({ query: 'latency' })
    /* No pager over a ranking: relevance has no second page the engine was
       asked for. */
    expect(document.querySelector('.kbchunkfoot')).toBeNull()
  })

  it('searches on its own once the typing stops', async () => {
    vi.useFakeTimers()
    try {
      const asked: unknown[] = []
      await openWithChunks([chunk()], 40, {
        chunks: async (_d: string, opts: { query?: string }) => {
          asked.push(opts)
          return { chunks: [chunk()], total: 1 }
        }
      })
      const before = asked.length
      const box = document.querySelector('.kbchunksearch') as HTMLInputElement

      await act(async () => {
        fireEvent.change(box, { target: { value: 'lat' } })
        vi.advanceTimersByTime(2000)
      })
      /* Still typing: the first keystrokes must not each spend a request. */
      expect(asked.length).toBe(before)

      await act(async () => {
        fireEvent.change(box, { target: { value: 'latency' } })
        vi.advanceTimersByTime(3000)
      })
      await act(async () => {})

      expect(asked[asked.length - 1]).toMatchObject({ query: 'latency' })
    } finally {
      vi.useRealTimers()
    }
  })

  it('goes back to the reading order the moment the box is emptied', async () => {
    /* Nothing to spend by waiting, and three seconds of a list that has
       stopped answering reads as a fault. */
    const asked: unknown[] = []
    await openWithChunks([chunk()], 40, {
      chunks: async (_d: string, opts: { query?: string; page?: number }) => {
        asked.push(opts)
        return { chunks: [chunk()], total: 40 }
      }
    })
    const box = document.querySelector('.kbchunksearch') as HTMLInputElement
    await act(async () => {
      fireEvent.change(box, { target: { value: 'latency' } })
      fireEvent.keyDown(box, { key: 'Enter' })
    })
    await act(async () => {})

    await act(async () => {
      fireEvent.change(box, { target: { value: '' } })
    })
    await act(async () => {})

    expect(asked[asked.length - 1]).toMatchObject({ page: 1 })
    expect(asked[asked.length - 1]).not.toHaveProperty('query')
  })

  it('shows a chunk whole or cut, and remembers which', async () => {
    await openWithChunks([chunk({ text: 'a'.repeat(400) })])

    expect(document.querySelector('.kbchunktx.cut')).not.toBeNull()
    await act(async () => {
      ;(screen.getByText('gui.kb.chunk_full').closest('button') as HTMLButtonElement).click()
    })
    expect(document.querySelector('.kbchunktx.cut')).toBeNull()
  })

  it('writes a chunk of its own, appended to the end', async () => {
    const written: string[] = []
    await openWithChunks([chunk()], 1, {
      createChunk: async (_d: string, text: string) => {
        written.push(text)
        return chunk({ chunk_index: 1, chunk_id: 'c2', text, manual: true })
      }
    })

    await act(async () => {
      ;(document.querySelector('.kbchunkadd') as HTMLButtonElement).click()
    })
    const body = document.querySelector('#kbchunkbody') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.change(body, { target: { value: 'a clause I typed' } })
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.create').closest('button') as HTMLButtonElement).click()
    })

    expect(written).toEqual(['a clause I typed'])
    expect(document.querySelector('.kbmodal')).toBeNull()
  })

  it('rewrites a chunk on a double click, and re-embeds it', async () => {
    /* A piece whose text changed and whose vector did not would be found by
       the old words and read as the new ones. */
    const saved: unknown[] = []
    await openWithChunks([chunk({ chunk_id: 'c1', text: 'as it was cut' })], 1, {
      updateChunk: async (d: string, id: string, text: string) => {
        saved.push([d, id, text])
        return chunk({ chunk_id: 'c9', text, manual: true })
      }
    })

    await act(async () => {
      fireEvent.doubleClick(document.querySelector('.kbchunktx') as HTMLElement)
    })
    const body = document.querySelector('#kbchunkbody') as HTMLTextAreaElement
    /* Opens on what is there, so an edit is an edit rather than a retype. */
    expect(body.value).toBe('as it was cut')

    await act(async () => {
      fireEvent.change(body, { target: { value: 'as I corrected it' } })
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.note_save').closest('button') as HTMLButtonElement).click()
    })

    expect(saved).toEqual([['d1', 'c1', 'as I corrected it']])
    expect(document.querySelector('.kbmodal')).toBeNull()
  })

  it('will not save an edit that changed nothing', async () => {
    await openWithChunks([chunk({ chunk_id: 'c1', text: 'as it was cut' })])

    await act(async () => {
      fireEvent.doubleClick(document.querySelector('.kbchunktx') as HTMLElement)
    })

    expect((screen.getByText('gui.kb.note_save').closest('button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('filters by state from the filter icon', async () => {
    const asked: unknown[] = []
    await openWithChunks([chunk()], 3, {
      chunks: async (_d: string, opts: { available?: boolean | null }) => {
        asked.push(opts)
        return { chunks: [chunk({ enabled: false })], total: 1 }
      }
    })

    await act(async () => {
      ;(document.querySelector('.kbfilter button') as HTMLButtonElement).click()
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.chunk_filter_off').closest('button') as HTMLButtonElement).click()
    })

    expect(asked[asked.length - 1]).toMatchObject({ available: false })
  })

  it('marks a chunk somebody wrote, and one that is off', async () => {
    await openWithChunks([chunk({ manual: true, enabled: false })])

    expect(screen.getByText('gui.kb.chunk_written')).toBeTruthy()
    expect(document.querySelector('.kbchunk.off')).not.toBeNull()
  })

  it('says a file has nothing indexed rather than showing an empty column', async () => {
    /* A document that failed, one still queued and one in a base with no model
       all land here, and an empty column would read as a bug. */
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd1', source: 'notes.md', status: 'failed' })],
      chunks: async () => ({ chunks: [], total: 0 })
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    await clickName('notes.md')
    await act(async () => {})

    expect(screen.getByText('gui.kb.chunks_none')).toBeTruthy()
  })

  it('opens the same panel from the row menu as from the name', async () => {
    source({
      bases: async () => [base({ id: 'b1' })],
      documents: async () => [doc({ id: 'd1', source: 'onboarding.md', status: 'ready' })],
      chunks: async () => ({
        chunks: [{ chunk_index: 0, total_chunks: 1, text: 'Only piece.', chunk_id: 'c1' }],
        total: 1
      })
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    await openRowMenu('onboarding.md')
    await act(async () => {
      ;(screen.getByText('gui.kb.doc_view_chunks').closest('button') as HTMLButtonElement).click()
    })
    await act(async () => {})

    expect(document.querySelector('.kbview')).not.toBeNull()
    expect(document.querySelector('.kbchunktx')?.textContent).toBe('Only piece.')
  })

  it('asks the gateway to convert the formats no browser draws', async () => {
    await openBase([doc({ id: 'd2', source: 'notice.doc', status: 'ready' })])

    await clickName('notice.doc')

    /* A legacy .doc has no reader in the browser and no pure-Python one worth
       trusting, so the gateway renders it with LibreOffice first. */
    expect(pdfAsked.url).toBe('/knowledge/file?document=d2&render=pdf')
  })

  it('takes the preview to the page a slide came from', async () => {
    /* A slide is a page: the preview is that deck rendered to PDF and the two
       agree page for page, so a piece can put the slide it was cut from in
       front of the reader rather than describing it. */
    await openBase([doc({ id: 'd9', source: 'deck.pptx', status: 'ready', chunk_count: 3 })])
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook' })],
      documents: async () => [doc({ id: 'd9', source: 'deck.pptx', status: 'ready', chunk_count: 3 })],
      chunks: async () => ({
        chunks: [
          { chunk_index: 0, total_chunks: 2, text: 'Opening', chunk_id: 'c1', page_number: 1 },
          { chunk_index: 1, total_chunks: 2, text: 'Closing', chunk_id: 'c2', page_number: 7 }
        ],
        total: 2
      })
    })
    const scrolled = recordScrolls()
    await clickName('deck.pptx')
    await act(async () => {})

    expect(pdfAsked.url).toBe('/knowledge/file?document=d9&render=pdf')

    await act(async () => {
      ;(document.querySelectorAll('.kbchunk')[1] as HTMLElement).click()
    })

    /* A slide knows its page and not a place on it, so the top of page seven
       is the whole of the right answer -- and six pages plus the gaps between
       them sit above it. */
    expect(scrolled.tops.at(-1)).toBe(6 * (PDF_PAGE_HEIGHT + 8))
    expect(document.querySelector('.kbpdfmark')).toBeNull()
    scrolled.restore()
    /* And the list says which piece the preview is answering to. */
    expect(document.querySelectorAll('.kbchunk')[1]?.classList.contains('at')).toBe(true)
    expect(document.querySelectorAll('.kbchunk')[0]?.classList.contains('at')).toBe(false)
  })

  it('does not move the preview when the click was a reader selecting words', async () => {
    /* A single click on prose is how a word gets selected, which is why the
       editor is on double-click. Taking that away would make the panel
       unreadable. */
    await openBase([doc({ id: 'd9', source: 'deck.pptx', status: 'ready', chunk_count: 1 })])
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook' })],
      documents: async () => [doc({ id: 'd9', source: 'deck.pptx', status: 'ready', chunk_count: 1 })],
      chunks: async () => ({
        chunks: [{ chunk_index: 0, total_chunks: 1, text: 'Opening', chunk_id: 'c1', page_number: 4 }],
        total: 1
      })
    })
    await clickName('deck.pptx')
    const selection = { toString: () => '  some selected words  ' } as Selection
    const spy = vi.spyOn(window, 'getSelection').mockReturnValue(selection)

    await act(async () => {
      ;(document.querySelector('.kbchunk') as HTMLElement).click()
    })

    expect(document.querySelector('.kbpdfmark')).toBeNull()
    expect(store.getState().previewFocus).toBe(0)
    spy.mockRestore()
  })

  it('leaves the preview alone for a chunk that names no page', async () => {
    /* A text file has no pages. Scrolling somewhere arbitrary would be worse
       than doing nothing. */
    await openBase([doc({ id: 'd8', source: 'notes.txt', status: 'ready', chunk_count: 1 })])
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook' })],
      documents: async () => [doc({ id: 'd8', source: 'notes.txt', status: 'ready', chunk_count: 1 })],
      chunks: async () => ({
        chunks: [{ chunk_index: 0, total_chunks: 1, text: 'Just text', chunk_id: 'c1' }],
        total: 1
      })
    })
    await clickName('notes.txt')

    await act(async () => {
      ;(document.querySelector('.kbchunk') as HTMLElement).click()
    })

    expect((document.querySelector('.kbframe') as HTMLIFrameElement).getAttribute('src')).toBe(
      '/knowledge/file?document=d8'
    )
    expect(document.querySelector('.kbchunk')?.classList.contains('at')).toBe(false)
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
      documents: async (id: string) => (id === 'b1' ? [doc({ id: 'd1', source: 'contract.pdf', status: 'ready' })] : [])
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await clickName('contract.pdf')
    await act(async () => {})
    expect(document.querySelector('.kbpdf')).not.toBeNull()

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
      text: async () => '# Onboarding\n\nRead **this** first.\n'
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
      text: async () => '# Hi\n\n<img src=x onerror="alert(1)">\n'
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
    expect(document.querySelector('.kbpdf')).not.toBeNull()
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
      doc({ id: 'd3', source: 'photo.png', status: 'ready' })
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
      doc({ id: 'd2', source: 'scan.pdf', status: 'ready' })
    ])

    const marks = [...document.querySelectorAll('.kbtable .kbico')]
    expect(marks.map(m => m.getAttribute('class'))).toEqual(['kbico kbf-sheet', 'kbico kbf-pdf'])
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
      ...over
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

    const items = [...document.querySelectorAll('.kbsrc .kbmenu .mi')]
    expect(items.map(b => b.textContent)).toEqual([
      'gui.kb.src_file',
      'gui.kb.src_note',
      'gui.kb.src_folder',
      'gui.kb.src_url'
    ])
    /* One drawing each, and hidden from a screen reader: the word beside it is
       already the name, and an icon read aloud after it says it twice. */
    for (const item of items) {
      const glyph = item.querySelector('svg.kbmi')
      expect(glyph).not.toBeNull()
      expect(glyph!.getAttribute('aria-hidden')).toBe('true')
    }
  })

  it('picks a folder with the attribute that makes a chooser a folder chooser', async () => {
    /* Without `webkitdirectory` this is the file input again; it is the whole
       difference between the two menu entries. */
    await openBase()

    const inputs = [...document.querySelectorAll('.kbsrc input[type="file"]')]
    expect(inputs.map(i => i.hasAttribute('webkitdirectory'))).toEqual([false, true])
    expect(inputs.every(i => i.hasAttribute('multiple'))).toBe(true)
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
      index: async (id: string) => doc({ id, source: id, status: 'ready' })
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
      index: async (id: string) => doc({ id, source: id, status: 'ready' })
    })

    await act(async () => {
      await store.uploadAll([file('a.md'), file('bad.md'), file('c.md')])
    })

    expect(store.getState().docs.map(d => d.source)).toEqual(['a.md', 'c.md'])
    expect(toasts()).toEqual(['nope'])
  })

  it('keeps only the files this build can index out of a folder', async () => {
    /* A source tree is mostly things no parser claims. Uploading them to watch
       them fail is not a file list. */
    await openBase()

    const kept = store.indexable([file('notes.md'), file('logo.png'), file('readme.TXT'), file('Makefile')])

    expect(kept.map(f => f.name)).toEqual(['notes.md', 'readme.TXT'])
  })

  it('takes any file into a base that was made without an embedding model', async () => {
    /* Such a base indexes nothing at all: it keeps documents to open and to
       hand to a turn, so "what a parser claims" is not a question about it,
       and filtering by that would throw away what it is for. */
    source({
      bases: async () => [base({ id: 'b1', name: 'scratch', embedding_model: '', dimensions: 0 })],
      documents: async () => [],
      status: async () => ({ configured: true, model: 'bge-m3', extensions: ['.md'] })
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    const kept = store.indexable([file('notes.md'), file('scan.pdf'), file('logo.png')])

    expect(kept.map(f => f.name)).toEqual(['notes.md', 'scan.pdf', 'logo.png'])
  })

  it('says so rather than starting when a folder holds nothing indexable', async () => {
    const tried: string[] = []
    await openBase({
      upload: async (_b: string, f: File) => {
        tried.push(f.name)
        return doc({ id: f.name })
      }
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
      }
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
      index: async (id: string) => doc({ id, source: 'Plan.md', origin: 'note', status: 'ready' })
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
        index: async (id: string) => ({ ...note, id, status: 'ready' })
      },
      [note]
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
      doc({ id: 'n1', source: 'Plan.md', origin: 'note', status: 'ready' })
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
      index: async (id: string) => doc({ id, source: 'Raven Docs.md', origin: 'url', status: 'ready' })
    })
    await openSourceMenu()
    await act(async () => {
      ;(screen.getByText('gui.kb.src_url').closest('button') as HTMLButtonElement).click()
    })

    await act(async () => {
      fireEvent.change(document.getElementById('kburl') as HTMLInputElement, {
        target: { value: '  https://example.com/docs  ' }
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
      addUrl: () => new Promise<KbDoc>(resolve => (release = resolve)),
      index: async (id: string) => doc({ id, origin: 'url', status: 'ready' })
    })
    await openSourceMenu()
    await act(async () => {
      ;(screen.getByText('gui.kb.src_url').closest('button') as HTMLButtonElement).click()
    })
    await act(async () => {
      fireEvent.change(document.getElementById('kburl') as HTMLInputElement, {
        target: { value: 'https://example.com/docs' }
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
      index: () => new Promise<KbDoc>(resolve => (finishIndex = resolve))
    })
    await openSourceMenu()
    await act(async () => {
      ;(screen.getByText('gui.kb.src_url').closest('button') as HTMLButtonElement).click()
    })
    await act(async () => {
      fireEvent.change(document.getElementById('kburl') as HTMLInputElement, {
        target: { value: 'https://example.com/docs' }
      })
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.url_add').closest('button') as HTMLButtonElement).click()
      await Promise.resolve()
    })

    expect(document.getElementById('kburl')).toBeNull()
    expect(store.getState().docs.map(d => d.status)).toEqual(['pending'])

    await act(async () => {
      finishIndex(doc({ id: 'u1', source: 'Docs.md', origin: 'url', status: 'ready' }))
      await Promise.resolve()
    })
    expect(store.getState().docs.map(d => d.status)).toEqual(['ready'])
  })

  it('leaves the url dialog open when the page could not be read', async () => {
    /* The address is usually nearly right; throwing it away with the dialog
       means typing it again. */
    await openBase({
      addUrl: async () => {
        throw new Error('the reader answered HTTP 404')
      }
    })
    await openSourceMenu()
    await act(async () => {
      ;(screen.getByText('gui.kb.src_url').closest('button') as HTMLButtonElement).click()
    })
    await act(async () => {
      fireEvent.change(document.getElementById('kburl') as HTMLInputElement, {
        target: { value: 'https://example.com/gone' }
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
      doc({ id: 'u1', source: 'Docs.md', origin: 'url', origin_ref: 'https://x', status: 'ready' })
    ])

    const rows = [...document.querySelectorAll('.kbtable .td.nm')]
    const types = rows.map(r => r.nextElementSibling?.textContent)
    expect(types).toEqual(['gui.kb.doc_type_file', 'gui.kb.doc_type_note', 'gui.kb.doc_type_url'])
    /* And each gets its own glyph, for the same reason. */
    const marks = [...document.querySelectorAll('.kbtable .kbico')].map(m => m.getAttribute('class'))
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
      status: 'ready'
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
      index: async (id: string) => doc({ id, source: id, status: 'ready' })
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
      {
        score: 0.8123,
        document_id: 'd1',
        text: 'the nearest passage',
        chunk_index: 7,
        total_chunks: 12,
        source: 'handbook.md'
      },
      {
        score: 0.4011,
        document_id: 'gone',
        text: 'from a deleted row',
        chunk_index: 0,
        total_chunks: 3,
        source: 'old.md'
      }
    ],
    search_ms: 6,
    embed_ms: 182.4
  }

  const openPanel = async (over: Partial<KnowledgeSource> = {}, over2: Partial<KbBase> = {}) => {
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook', ...over2 })],
      documents: async () => [doc({ id: 'd1', source: 'handbook.md', status: 'ready' })],
      ...over
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
    expect((document.querySelector('.kbstats span') as HTMLElement).textContent).toBe('gui.kb.recall_ms {"ms":6}')
    expect((document.querySelector('.kbstats span') as HTMLElement).title).toContain('182.4')
  })

  it('asks for as many chunks as the base is configured for', async () => {
    const asked: Array<number | undefined> = []
    await openPanel(
      {
        search: async (_b: string[], _q: string, topK?: number) => {
          asked.push(topK)
          return HITS
        }
      },
      { top_k: 9 }
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

    const names = [...document.querySelectorAll('.kbhitnm')].map(e => e.textContent)
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
      }
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
      }
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
    expect((document.querySelector('.kbask .kbname') as HTMLInputElement).value).toBe('what is the leave policy')
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
      ...over
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await act(async () => {
      ;(screen.getByLabelText('gui.kb.settings') as HTMLButtonElement).click()
    })
    return saved
  }

  const field = (label: string): HTMLElement => screen.getByText(label).closest('.kbset') as HTMLElement

  it('shows the fields the design asks for, and no rerank model', async () => {
    await openSettings()

    for (const label of [
      'gui.kb.set_proc',
      'gui.kb.set_embed',
      'gui.kb.set_topk',
      'gui.kb.set_smart',
      'gui.kb.set_sep',
      'gui.kb.set_size',
      'gui.kb.set_lap'
    ]) {
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
    expect(helps.length).toBe(9)
    for (const help of helps) {
      expect(help.getAttribute('title')).toBeTruthy()
      /* Reachable without a pointer, or the sentence only exists for people
         who can hover. */
      expect(help.getAttribute('tabindex')).toBe('0')
      expect(help.getAttribute('aria-label')).toBe(help.getAttribute('title'))
    }
  })

  it('says a base cannot embed, where a reader is looking at it', async () => {
    /* Nothing it holds can be indexed or searched until the model it was built
       with can be reached again, and a line in the gateway log is not where
       the person who can fix that is looking. */
    source({
      bases: async () => [
        base({ id: 'b1', embedding_model: 'BAAI/bge-large-zh-v1.5', embedding_reach: 'no_provider' })
      ],
      documents: async () => []
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    const warning = document.querySelector('.kbunreach') as HTMLButtonElement
    expect(warning).not.toBeNull()
    expect(warning.textContent).toBe('gui.kb.reach_no_provider')
    expect(warning.title).toContain('BAAI/bge-large-zh-v1.5')

    /* And it opens the panel that holds the repair. */
    await act(async () => {
      warning.click()
    })
    expect(document.querySelector('.kbsets')).not.toBeNull()
  })

  it('says nothing when the model is reachable', async () => {
    source({ bases: async () => [base({ id: 'b1' })], documents: async () => [] })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })

    expect(document.querySelector('.kbunreach')).toBeNull()
  })

  it('shows the provider and the model as one endpoint', async () => {
    /* An embedding endpoint is a provider and a model together -- a model id
       does not name a credential -- so they read as one value rather than as
       two fields to reconcile. A provider with no credential is not offered:
       that would be offering a repair that fails. */
    await openSettings({}, { embedding_model: 'BAAI/bge-large-zh-v1.5', embedding_provider: 'siliconflow' })

    const picker = field('gui.kb.set_embed').querySelector('select') as HTMLSelectElement
    expect(picker.value).toBe('siliconflow::BAAI/bge-large-zh-v1.5')
    const offered = [...picker.options].map(o => o.value)
    expect(offered).toContain('')
    expect(offered).toContain('dashscope::text-embedding-v4')
  })

  it('offers a base with no embedding a model to turn it on with', async () => {
    /* It used to offer nothing at all: the choice was made once, at creation,
       and a base made without vectors stayed that way or was rebuilt by hand.
       It is the same rebuild as any other switch, so it is the same control. */
    await openSettings({}, { embedding_model: '' })

    const picker = field('gui.kb.set_embed').querySelector('select') as HTMLSelectElement
    expect(picker.value).toBe('')
    expect([...picker.options].map(o => o.value)).toContain('siliconflow::BAAI/bge-m3')
  })

  it('keeps a base on its own model when it is served somewhere the list does not offer', async () => {
    /* A select whose value matches no option draws as empty, which would tell
       a reader their base has no model when it has one. */
    await openSettings({}, { embedding_model: 'house-embed', embedding_provider: 'custom' })

    const picker = field('gui.kb.set_embed').querySelector('select') as HTMLSelectElement
    expect(picker.value).toBe('custom::house-embed')
    expect([...picker.options].map(o => o.textContent)).toContain('house-embed')
  })

  it('reads a prefixed id and the recorded spelling as one model', async () => {
    /* A provider stores its models under its own prefix and the prefix comes
       off before the request goes out, so the picker offers
       `siliconflow/BAAI/bge-m3` for a base that records `BAAI/bge-m3`. Read as
       two, the reader is offered both and picking the one they are already on
       counts as a rebuild. */
    const saved = await openSettings(
      {
        embeddingModels: async () => [{ id: 'siliconflow', name: 'SiliconFlow', models: ['siliconflow/BAAI/bge-m3'] }]
      },
      { embedding_model: 'BAAI/bge-m3', embedding_provider: 'siliconflow', documents: 3 }
    )

    const picker = field('gui.kb.set_embed').querySelector('select') as HTMLSelectElement
    expect(picker.value).toBe('siliconflow::siliconflow/BAAI/bge-m3')
    expect([...picker.options].map(o => o.value)).toEqual(['', 'siliconflow::siliconflow/BAAI/bge-m3'])
    expect(field('gui.kb.set_embed').textContent).not.toContain('gui.kb.set_embed_moved')

    await act(async () => {
      fireEvent.change(picker, { target: { value: 'siliconflow::siliconflow/BAAI/bge-m3' } })
    })
    await act(async () => {
      screen.getByText('gui.kb.set_save').click()
    })

    expect(confirms).toEqual([])
    expect(saved[0]).not.toHaveProperty('embedding_model')
  })

  it('records a provider for a base that has none without calling it a rebuild', async () => {
    /* A base written before providers were recorded names a model and nobody
       to serve it, which means "wherever this is configured". Naming one is
       the repair it has always been, not a move -- the vectors are the ones
       that model makes wherever it is reached from. */
    const saved = await openSettings({}, { embedding_model: 'BAAI/bge-m3', embedding_provider: '', documents: 3 })

    const picker = field('gui.kb.set_embed').querySelector('select') as HTMLSelectElement
    expect(picker.value).toBe('siliconflow::BAAI/bge-m3')
    await act(async () => {
      fireEvent.change(picker, { target: { value: 'siliconflow::BAAI/bge-m3' } })
    })
    await act(async () => {
      screen.getByText('gui.kb.set_save').click()
    })

    expect(confirms).toEqual([])
    expect(saved[0]).toMatchObject({ embedding_provider: 'siliconflow' })
    expect(saved[0]).not.toHaveProperty('embedding_model')
  })

  it('tells one model from another that ends the same way', async () => {
    /* `BAAI/bge-m3` and `bge-m3` are two models; only a provider's own prefix
       comes off on the way to the endpoint. */
    await openSettings({}, { embedding_model: 'bge-m3', embedding_provider: '' })

    const picker = field('gui.kb.set_embed').querySelector('select') as HTMLSelectElement
    expect(picker.value).toBe('::bge-m3')
    expect([...picker.options].map(o => o.textContent)).toContain('bge-m3')
  })

  it('sends the model only when it moved, and asks first', async () => {
    /* A panel saving a slider must not drop the index, and a reader changing
       the model has to be told what it costs before it happens rather than
       after. */
    const saved = await openSettings(
      {},
      { embedding_model: 'BAAI/bge-large-zh-v1.5', embedding_provider: 'siliconflow', documents: 3 }
    )

    await act(async () => {
      ;(document.querySelector('.kbsets input[type="range"]') as HTMLInputElement).value = '9'
      fireEvent.change(document.querySelector('.kbsets input[type="range"]') as HTMLInputElement, {
        target: { value: '9' }
      })
    })
    await act(async () => {
      screen.getByText('gui.kb.set_save').click()
    })
    expect(saved[0]).not.toHaveProperty('embedding_model')
    expect(confirms).toEqual([])
  })

  it('rebuilds the base when another model is picked', async () => {
    const saved = await openSettings(
      {},
      { embedding_model: 'BAAI/bge-large-zh-v1.5', embedding_provider: 'siliconflow', documents: 3 }
    )

    const picker = field('gui.kb.set_embed').querySelector('select') as HTMLSelectElement
    await act(async () => {
      fireEvent.change(picker, { target: { value: 'dashscope::text-embedding-v4' } })
    })
    /* Said where the picking happens, before the confirmation says it again
       with the number of documents. */
    expect(field('gui.kb.set_embed').textContent).toContain('gui.kb.set_embed_moved')

    await act(async () => {
      screen.getByText('gui.kb.set_save').click()
    })

    expect(confirms[0]).toContain('"count":3')
    expect(saved[0]).toMatchObject({
      embedding_model: 'text-embedding-v4',
      embedding_provider: 'dashscope'
    })
  })

  it('re-reads the file list after a rebuild', async () => {
    /* The rows are all back in the queue with no chunks. A list still saying
       ready beside a count of zero describes the base as it was a second
       ago. */
    let reads = 0
    await openSettings(
      {
        documents: async () => {
          reads += 1
          return [doc({ id: 'd1', status: reads > 1 ? 'pending' : 'ready', chunk_count: reads > 1 ? 0 : 4 })]
        }
      },
      { embedding_model: 'BAAI/bge-m3', embedding_provider: 'siliconflow', documents: 1 }
    )

    await act(async () => {
      fireEvent.change(field('gui.kb.set_embed').querySelector('select') as HTMLSelectElement, {
        target: { value: 'dashscope::text-embedding-v4' }
      })
    })
    await act(async () => {
      screen.getByText('gui.kb.set_save').click()
    })

    expect(reads).toBeGreaterThan(1)
    expect(screen.getByText('gui.kb.doc_pending')).toBeTruthy()
  })

  it('asks nothing of a base with no documents', async () => {
    /* There is no index to lose, and a confirmation nobody needs is one more
       click through a dialog. */
    const saved = await openSettings(
      {},
      { embedding_model: 'BAAI/bge-m3', embedding_provider: 'siliconflow', documents: 0 }
    )

    await act(async () => {
      fireEvent.change(field('gui.kb.set_embed').querySelector('select') as HTMLSelectElement, {
        target: { value: '' }
      })
    })
    await act(async () => {
      screen.getByText('gui.kb.set_save').click()
    })

    expect(confirms).toEqual([])
    expect(saved[0]).toMatchObject({ embedding_model: '' })
  })

  it('lists the processors it will offer, every one of them unavailable', async () => {
    /* The list is the answer to "what will this eventually do"; a lone "Don't
       use" in a select answers nothing. Nothing is wired, so every row says
       why it cannot be picked rather than looking like a choice. */
    await openSettings()

    expect((field('gui.kb.set_proc').querySelector('.kbpick') as HTMLElement).textContent).toContain(
      'gui.kb.set_proc_off'
    )
    await act(async () => {
      ;(document.querySelector('.kbpick') as HTMLButtonElement).click()
    })

    const rows = [...document.querySelectorAll('.kbprocr')]
    expect(rows.map(r => (r.querySelector('.nm') as HTMLElement).textContent)).toEqual([
      'gui.kb.proc_local',
      'PaddleOCR',
      'MinerU',
      'Doc2X',
      'Mistral',
      'gui.kb.proc_settings',
      'gui.kb.set_proc_off'
    ])
    expect(rows.every(r => r.getAttribute('aria-disabled') === 'true')).toBe(true)
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
      String(store.DEFAULTS.top_k)
    )
    expect((field('gui.kb.set_size').querySelector('input') as HTMLInputElement).value).toBe(
      String(store.DEFAULTS.chunk_size)
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
    const saved = await openSettings(
      {},
      { top_k: 40, chunk_size: 2048, chunk_overlap: 400, table_context_size: 10, image_context_size: 10 }
    )

    await act(async () => {
      ;(screen.getByText('gui.kb.set_restore').closest('button') as HTMLButtonElement).click()
    })

    expect((field('gui.kb.set_topk').querySelector('input') as HTMLInputElement).value).toBe(
      String(store.DEFAULTS.top_k)
    )
    expect((field('gui.kb.set_size').querySelector('input') as HTMLInputElement).value).toBe(
      String(store.DEFAULTS.chunk_size)
    )
    expect((field('gui.kb.set_lap').querySelector('input') as HTMLInputElement).value).toBe(
      String(store.DEFAULTS.chunk_overlap)
    )
    /* The two context sizes too, and from DEFAULTS rather than from zero:
       written as zero this button turned the feature off instead of restoring
       it, which is the opposite of what it says. */
    expect((field('gui.kb.set_tablectx').querySelector('input') as HTMLInputElement).value).toBe(
      String(store.DEFAULTS.table_context_size)
    )
    expect((field('gui.kb.set_imagectx').querySelector('input') as HTMLInputElement).value).toBe(
      String(store.DEFAULTS.image_context_size)
    )
    ;(expect(store.DEFAULTS.table_context_size).toBeGreaterThan(0), 'or the check above proves nothing')
    /* Restoring is an edit like any other: nothing is written until Save. */
    expect(saved).toEqual([])
  })

  it('says the strategy switch is not in force rather than offering a dead one', async () => {
    /* Every base is cut the naive way while the structural chunker is
       reworked. A switch that silently decides nothing is how the other four
       settings on this panel spent their first release. */
    await openSettings({}, { smart_chunking: true })

    expect(field('gui.kb.set_smart').querySelector('.kbtog')).toBeNull()
    expect(field('gui.kb.set_smart').textContent).toContain('gui.kb.set_smart_soon')
    /* And the delimiters it would have replaced are in force, so the field
       that sets them is live. */
    expect((field('gui.kb.set_sep').querySelector('input') as HTMLInputElement).disabled).toBe(false)
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
    doc({ id: 'd3', source: 'notes.md', status: 'failed', error: 'endpoint said 400' })
  ]

  const openWith = async (over: Partial<KnowledgeSource> = {}, docs: KbDoc[] = THREE) => {
    /* `show` drops a toast when the page's standing host is absent, and
       whether a previous test left one behind is not what these assert. */
    toastHost()
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook' })],
      documents: async () => docs,
      ...over
    })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
  }

  const ticks = (): HTMLInputElement[] =>
    [...document.querySelectorAll('.kbtable .td.kbtick input')] as HTMLInputElement[]
  const headTick = (): HTMLInputElement => document.querySelector('.kbtable .th.kbtick input') as HTMLInputElement
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
    /* The table is a grid, so a row is seven sibling cells rather than an
       element that could carry the state; marking only some would stripe it. */
    await openWith()
    await tick(1)

    const marked = [...document.querySelectorAll('.kbtable .td.kbsel')]
    expect(marked.length).toBe(7)
    expect(marked.map(c => c.textContent).join('')).toContain('report.docx')
  })

  it('ticks every row from the header, and clears from it', async () => {
    await openWith()

    await act(async () => {
      fireEvent.click(headTick())
    })
    expect(ticks().every(t => t.checked)).toBe(true)
    expect(headTick().checked).toBe(true)

    /* Pressing it again clears rather than re-picking, which is what every
       list of checkboxes does. */
    await act(async () => {
      fireEvent.click(headTick())
    })
    expect(ticks().some(t => t.checked)).toBe(false)
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
      }
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
    const byId = Object.fromEntries(store.getState().docs.map(d => [d.id, d.status]))
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
      documents: async () => THREE.filter(d => !removed.includes(d.id)),
      removeDoc: async (id: string) => {
        removed.push(id)
      }
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
      removeDoc: async () => {}
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

describe('renaming and deleting a base', () => {
  const openRail = async (over: Partial<KnowledgeSource> = {}) => {
    toastHost()
    source({
      bases: async () => [base({ id: 'b1', name: 'handbook', documents: 8 })],
      documents: async () => [],
      ...over
    })
    await mount()
  }

  const openBaseMenu = async () => {
    await act(async () => {
      ;(document.querySelector('.kbrail .kbops .dots') as HTMLButtonElement).click()
    })
  }

  it('puts both actions behind the row, and neither in front of it', async () => {
    /* A column of dots down an untouched list is noise, so the control shows
       on the row being pointed at or on the open one. */
    await openRail()

    expect(document.querySelector('.kbrail .kbmenu')).toBeNull()
    await openBaseMenu()
    const items = [...document.querySelectorAll('.kbrail .kbmenu .mi')].map(b => b.textContent)
    expect(items).toEqual(['gui.kb.rename', 'gui.kb.delete_base'])
  })

  it('still opens the base when the row itself is clicked', async () => {
    /* The menu is a control inside the row, and a button cannot hold another,
       so the row stopped being one. It has to keep doing what it did. */
    await openRail()

    await act(async () => {
      ;(document.querySelector('.kbrail .kbopenb') as HTMLButtonElement).click()
    })

    expect(store.getState().openId).toBe('b1')
  })

  it('renames a base and keeps the row it renamed', async () => {
    const asked: Array<[string, string]> = []
    await openRail({
      rename: async (id: string, name: string) => {
        asked.push([id, name])
        return base({ id, name, documents: 8 })
      }
    })

    await openBaseMenu()
    await act(async () => {
      ;(screen.getByText('gui.kb.rename').closest('button') as HTMLButtonElement).click()
    })
    const field = document.getElementById('kbrename') as HTMLInputElement
    /* Opens on the name it has, selected, so replacing it is one gesture. */
    expect(field.value).toBe('handbook')

    await act(async () => {
      fireEvent.change(field, { target: { value: '  staff handbook  ' } })
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.save').closest('button') as HTMLButtonElement).click()
      await Promise.resolve()
    })

    expect(asked).toEqual([['b1', 'staff handbook']])
    expect(store.getState().bases[0]!.name).toBe('staff handbook')
    expect(document.getElementById('kbrename')).toBeNull()
  })

  it('spends no request on a name that did not change', async () => {
    let calls = 0
    await openRail({
      rename: async (id: string, name: string) => {
        calls += 1
        return base({ id, name })
      }
    })

    await openBaseMenu()
    await act(async () => {
      ;(screen.getByText('gui.kb.rename').closest('button') as HTMLButtonElement).click()
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.save').closest('button') as HTMLButtonElement).click()
      await Promise.resolve()
    })

    expect(calls).toBe(0)
    expect(document.getElementById('kbrename')).toBeNull()
  })

  it('keeps the dialog open and says why when the name is taken', async () => {
    /* The engine refuses a name another base holds -- the same rule creation
       applies, or renaming would be the way around it. Throwing the typed name
       away with the dialog would mean typing it again. */
    await openRail({
      rename: async () => {
        throw new Error('a knowledge base called staff handbook already exists')
      }
    })

    await openBaseMenu()
    await act(async () => {
      ;(screen.getByText('gui.kb.rename').closest('button') as HTMLButtonElement).click()
    })
    await act(async () => {
      fireEvent.change(document.getElementById('kbrename') as HTMLInputElement, {
        target: { value: 'staff handbook' }
      })
    })
    await act(async () => {
      ;(screen.getByText('gui.kb.save').closest('button') as HTMLButtonElement).click()
      await Promise.resolve()
    })

    expect(toasts()).toEqual(['a knowledge base called staff handbook already exists'])
    expect(document.getElementById('kbrename')).not.toBeNull()
  })

  it('warns before deleting, naming the base and what goes with it', async () => {
    const removed: string[] = []
    await openRail({
      remove: async (id: string) => {
        removed.push(id)
        return {}
      }
    })

    await openBaseMenu()
    await act(async () => {
      ;(screen.getByText('gui.kb.delete_base').closest('button') as HTMLButtonElement).click()
      await Promise.resolve()
    })

    /* Asked, not done: everything in the base goes with it. The prompt names
       the base and how many documents are in it. */
    expect(confirms).toEqual(['gui.kb.delete_body {"name":"handbook","n":8}'])
    expect(removed).toEqual(['b1'])
  })

  it('leaves the panel behind when the base it belonged to is deleted', async () => {
    await openRail({ remove: async () => ({}) })
    await act(async () => {
      await store.open_('b1')
    })
    expect(store.getState().openId).toBe('b1')

    await openBaseMenu()
    await act(async () => {
      ;(screen.getByText('gui.kb.delete_base').closest('button') as HTMLButtonElement).click()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(store.getState().openId).toBeNull()
  })
})
