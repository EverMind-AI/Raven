// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
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
  toastHost().replaceChildren()
  confirms.length = 0
  delete (window as { DS?: unknown }).DS
})

describe('the knowledge page', () => {
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
      await store.search('what')
    })
    expect(screen.getByText('the answer')).toBeTruthy()
    /* Two decimals: a similarity is for ranking by eye. */
    expect(screen.getByText('0.81')).toBeTruthy()
    await act(async () => {
      await store.search('  ')
    })
    expect(screen.queryByText('the answer')).toBeNull()
    expect(screen.getByText('onboarding.md')).toBeTruthy()
  })

  it('tells an empty result apart from not having asked', async () => {
    source({ bases: async () => [base({ id: 'b1' })], search: async () => [] })
    await mount()
    await act(async () => {
      await store.open_('b1')
    })
    await act(async () => {
      await store.search('nothing here')
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
    const slow = store.search('what')
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
