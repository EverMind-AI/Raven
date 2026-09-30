// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as menu from '../../state/menu'
import { resetSources, sources } from '../../state/sources'
import { KnowledgeApp } from './KnowledgePage'
import * as store from './store'

import type { KbBase, KbChunk, KbChunkAsk, KbModel, KbPage, KbDoc, KbFolder, KbSettings } from './types'

/* The page a reader sees: a bar of tabs over a grid of cards.
 *
 * The store's own tests cover which rows a tab shows; these cover what a card
 * offers and what pressing it asks for.
 */

function base(over: Partial<KbBase> & { id: string }): KbBase {
  return {
    name: over.id,
    description: '',
    documents: 0,
    created_at: '2026-01-01T00:00:00Z',
    pinned: false,
    starred: false,
    embedding_model: 'embed',
    embedding_provider: '',
    chunks: 0,
    top_k: 6,
    smart_chunking: true,
    separator: '\n\n',
    chunk_size: 2048,
    chunk_overlap: 215,
    table_context_size: 64,
    image_context_size: 64,
    ...over,
  }
}

let docs: KbDoc[] = []
let folders: KbFolder[] = []
let chunks: KbChunk[] = []
let sheets: KbPage[] = []
const offers: KbModel[] = [
  { id: 'bge-m3', label: 'BGE M3', provider: 'ollama', providerName: 'Ollama' },
  { id: 'text-embedding-3-small', label: 'text-embedding-3-small', provider: 'openai', providerName: 'OpenAI' },
]
/** What each read of the list asked for, in order. */
const asked: KbChunkAsk[] = []
const switched: Array<[string[], boolean]> = []
const killed: string[][] = []
const moved: Array<[string, string]> = []
const added: string[] = []
const removedDocs: string[] = []
const wrote: Array<[string, KbSettings]> = []
const removed: string[] = []

/** A row for a document that has just arrived, as the server would answer. */
function newDoc(source: string): KbDoc {
  const made: KbDoc = {
    id: `d-${source}`,
    source,
    status: 'pending',
    chunk_count: 0,
    size: 0,
    media_type: '',
    updated_at: '',
    folder_id: '',
    error: '',
    warning: '',
  }
  docs = [...docs, made]
  return made
}

function install(rows: KbBase[]): void {
  sources.knowledge = {
    bases: async () => rows,
    documents: async () => docs,
    reindex: async (id) => ({ ...docs.find((d) => d.id === id)!, status: 'ready' }),
    removeDoc: async (id) => {
      removedDocs.push(id)
    },
    models: async () => offers,
    pages: async () => sheets,
    chunks: async (_id, ask) => {
      asked.push(ask)
      /* As the server does it: the filter narrows first, then the page is cut
         out of what is left, and `total` counts what the filter admits rather
         than what came back. */
      const q = ask.query.trim().toLowerCase()
      const kept = chunks
        .filter((row) => (ask.available === null ? true : row.enabled === ask.available))
        .filter((row) => (q ? row.text.toLowerCase().includes(q) : true))
      const from = (ask.page - 1) * store.CHUNK_PAGE
      return { chunks: kept.slice(from, from + store.CHUNK_PAGE), total: kept.length }
    },
    switchChunks: async (_id, ids, enabled) => {
      switched.push([ids, enabled])
      chunks = chunks.map((row) => (ids.includes(row.chunk_id) ? { ...row, enabled } : row))
      return ids.length
    },
    deleteChunks: async (_id, ids) => {
      killed.push(ids)
      chunks = chunks.filter((row) => !ids.includes(row.chunk_id))
      return chunks.length
    },
    createChunk: async (_id, text) => {
      const made = chunk({ chunk_index: chunks.length, text, chunk_id: `c${chunks.length}`, manual: true })
      chunks = [...chunks, made]
      return made
    },
    updateChunk: async (_id, chunkId, text) => {
      chunks = chunks.map((row) => (row.chunk_id === chunkId ? { ...row, text } : row))
      return chunks.find((row) => row.chunk_id === chunkId)!
    },
    folders: async () => folders,
    createFolder: async (_baseId, name) => {
      const made = { id: `f-${name}`, name, documents: 0 }
      folders = [...folders, made]
      return made
    },
    removeFolder: async (id) => {
      folders = folders.filter((row) => row.id !== id)
    },
    move: async (id, folderId) => {
      moved.push([id, folderId])
      docs = docs.map((row) => (row.id === id ? { ...row, folder_id: folderId } : row))
      return docs.find((row) => row.id === id)!
    },
    upload: async (_baseId, name) => {
      added.push(name)
      return newDoc(name)
    },
    addUrl: async (_baseId, url) => {
      added.push(url)
      return newDoc(url)
    },
    addNote: async (_baseId, title) => {
      added.push(title)
      return newDoc(title)
    },
    create: async (name, description) => base({ id: name, name, description }),
    settings: async (id, settings) => {
      wrote.push([id, settings])
      return { ...rows.find((row) => row.id === id)!, ...settings } as KbBase
    },
    remove: async (id) => {
      removed.push(id)
    },
  }
}

async function mount(rows: KbBase[]): Promise<void> {
  install(rows)
  /* The standing host src/App.tsx renders; `menu.show` is a no-op without it,
     and this page raises its card menu there rather than inside the card. */
  const host = document.createElement('div')
  host.id = 'menu'
  document.body.appendChild(host)
  render(<KnowledgeApp />)
  await act(async () => {
    await store.load()
  })
}

/** One document, with only what a test cares about spelled out. */
function doc(over: Partial<KbDoc> & { id: string }): KbDoc {
  return {
    source: over.id,
    status: 'ready',
    chunk_count: 1,
    size: 10,
    media_type: '',
    updated_at: '',
    folder_id: '',
    error: '',
    warning: '',
    ...over,
  }
}

/* Put words in a field, as a reader would. Its own act() rather than one
   shared with the key that follows: the handler that key runs is the one from
   the render before it, and a field typed into and submitted in a single act
   submits what was there before the typing. */
async function type(field: HTMLInputElement, text: string): Promise<void> {
  await act(async () => {
    fireEvent.change(field, { target: { value: text } })
  })
}

/** One indexed piece, with only what a test cares about spelled out. */
function chunk(over: Partial<KbChunk> & { chunk_index: number }): KbChunk {
  return {
    total_chunks: 2,
    text: `piece ${over.chunk_index}`,
    layout_type: '',
    page_number: null,
    page_end: null,
    heading_path: [],
    chunk_id: `c${over.chunk_index}`,
    enabled: true,
    manual: false,
    has_crop: false,
    regions: [],
    ...over,
  }
}

/** One document, as a row or as a card -- whichever the view is in. */
const docAt = (at = 0): HTMLElement =>
  [...document.querySelectorAll('.knowledge-doc, .knowledge-doccard')][at] as HTMLElement

/** Into the first document on the page, with its pieces read. */
async function openDoc(): Promise<void> {
  await act(async () => {
    docAt().click()
  })
  await vi.waitFor(() => expect(store.get().chunks).not.toBeNull())
}

/** Into the one base on the page, which is where documents are. */
async function open(): Promise<void> {
  await act(async () => {
    ;(document.querySelector('.knowledge-card') as HTMLElement).click()
  })
}

/** The labels the card menu is offering, in order. */
const rows = (): string[] =>
  menu.get().items.map((item) => (item === '-' ? '-' : item.label))

/** Pick one of them by label, the way `menu.pick` does. */
async function pick(label: string): Promise<void> {
  const item = menu.get().items.find((i) => i !== '-' && i.label === label)
  await act(async () => {
    menu.pick(item as Exclude<typeof item, '-' | undefined>)
  })
}

beforeEach(() => {
  store._resetForTests()
  menu._resetForTests()
  document.getElementById('menu')?.remove()
  wrote.length = 0
  removed.length = 0
  docs = []
  folders = []
  chunks = []
  sheets = []
  asked.length = 0
  switched.length = 0
  killed.length = 0
  moved.length = 0
  added.length = 0
  removedDocs.length = 0
})

afterEach(() => {
  cleanup()
  resetSources()
  vi.restoreAllMocks()
})

describe('the knowledge page', () => {
  it('draws a card for every base', async () => {
    await mount([base({ id: 'a', name: 'handbook' }), base({ id: 'b', name: 'runbook' })])

    expect(document.querySelectorAll('.knowledge-card')).toHaveLength(2)
    expect(screen.getByText('handbook')).toBeTruthy()
  })

  it('says a base has no description rather than leaving a gap', async () => {
    /* An empty space where the line should be reads as a card that failed to
       load, which is a different thing from one nobody described. */
    await mount([base({ id: 'a' })])

    expect(document.querySelector('.knowledge-desc')?.classList.contains('knowledge-none')).toBe(true)
  })

  it('warns on a base that cannot be searched', async () => {
    await mount([base({ id: 'a', embedding_model: '' })])

    expect(document.querySelector('.knowledge-warn')).not.toBeNull()
  })

  it('stars a base from the corner of its card', async () => {
    await mount([base({ id: 'a' })])

    await act(async () => {
      ;(document.querySelector('.knowledge-star') as HTMLButtonElement).click()
    })

    expect(wrote).toEqual([['a', { starred: true }]])
  })

  it('offers pin, settings and delete behind the dots, and nothing else', async () => {
    /* Three items. Duplicate is deliberately not among them: a copy of a base
       is a copy of everything indexed in it, which is not a menu item. */
    await mount([base({ id: 'a' })])

    await act(async () => {
      ;(document.querySelector('.knowledge-dots') as HTMLButtonElement).click()
    })

    expect(rows()).toEqual(['Pin to top', 'Settings', 'Delete'])
  })

  it('asks a second time before it deletes', async () => {
    await mount([base({ id: 'a' })])
    await act(async () => {
      ;(document.querySelector('.knowledge-dots') as HTMLButtonElement).click()
    })

    await pick('Delete')
    expect(removed).toEqual([])
    /* The menu comes back with the row reworded rather than the base going. */
    expect(rows()).toEqual(['Pin to top', 'Settings', 'Delete, really'])

    await pick('Delete, really')
    expect(removed).toEqual(['a'])
  })

  it('switches what the grid shows when a tab is picked', async () => {
    await mount([base({ id: 'a', starred: true }), base({ id: 'b' })])

    await act(async () => {
      ;(screen.getByText('Starred') as HTMLButtonElement).click()
    })

    expect(document.querySelectorAll('.knowledge-card')).toHaveLength(1)
  })

  it('says which tab is current', async () => {
    await mount([base({ id: 'a' })])

    const tab = (name: string): HTMLElement => screen.getByText(name)
    expect(tab('All').getAttribute('aria-selected')).toBe('true')
    await act(async () => {
      ;(tab('Recent') as HTMLButtonElement).click()
    })
    expect(tab('Recent').getAttribute('aria-selected')).toBe('true')
    expect(tab('All').getAttribute('aria-selected')).toBe('false')
  })

  it('says an empty library is empty, and says so differently on the starred tab', async () => {
    await mount([])
    expect(screen.getByText('No knowledge bases yet')).toBeTruthy()

    await act(async () => {
      ;(screen.getByText('Starred') as HTMLButtonElement).click()
    })
    expect(screen.getByText('Nothing starred yet')).toBeTruthy()
  })

  it('goes into a base when its card is clicked', async () => {
    docs = [{ id: 'd1', source: 'handbook.pdf', status: 'ready', chunk_count: 12, size: 2048, media_type: 'application/pdf', updated_at: '2026-09-24T14:55:00Z', folder_id: '', error: '', warning: '' }]
    await mount([base({ id: 'a', name: 'handbook' })])

    await act(async () => {
      ;(document.querySelector('.knowledge-card') as HTMLElement).click()
    })

    /* The base cards are gone; what is in their place is the documents. */
    expect(document.querySelector('.knowledge-card')).toBeNull()
    expect(document.querySelector('.knowledge-doc')).not.toBeNull()
    expect(screen.getByText('handbook.pdf')).toBeTruthy()
    /* The path says where the reader is and how to get back. */
    expect(document.querySelector('.knowledge-path')).not.toBeNull()
    expect(screen.getByText('Documents')).toBeTruthy()
  })

  it('does not open the base when the star or the dots were what was pressed', async () => {
    /* Both sit on the card, so without stopping the click a reader starring a
       base would also be taken into it. */
    await mount([base({ id: 'a' })])

    await act(async () => {
      ;(document.querySelector('.knowledge-star') as HTMLButtonElement).click()
    })
    expect(store.get().opened).toBeNull()

    await act(async () => {
      ;(document.querySelector('.knowledge-dots') as HTMLButtonElement).click()
    })
    expect(store.get().opened).toBeNull()
  })

  it('comes back out to the grid from the path', async () => {
    await mount([base({ id: 'a', name: 'handbook' })])
    await act(async () => {
      ;(document.querySelector('.knowledge-card') as HTMLElement).click()
    })

    await act(async () => {
      ;(document.querySelector('.knowledge-crumb') as HTMLButtonElement).click()
    })

    expect(document.querySelector('.knowledge-card')).not.toBeNull()
    expect(store.get().opened).toBeNull()
  })

  it('switches base from the path, with the current one ticked', async () => {
    await mount([base({ id: 'a', name: 'alpha' }), base({ id: 'b', name: 'beta' })])
    await act(async () => {
      ;(document.querySelectorAll('.knowledge-card')[0] as HTMLElement).click()
    })

    await act(async () => {
      ;(document.querySelector('.knowledge-switch') as HTMLButtonElement).click()
    })
    expect(rows()).toEqual(['alpha', 'beta'])
    expect(menu.get().items.map((i) => (i === '-' ? null : i.on))).toEqual([true, false])

    await pick('beta')
    expect(store.get().opened!.name).toBe('beta')
  })

  it('says an empty base is empty rather than still reading', async () => {
    await mount([base({ id: 'a' })])

    await act(async () => {
      ;(document.querySelector('.knowledge-card') as HTMLElement).click()
    })

    expect(screen.getByText('No documents yet')).toBeTruthy()
  })

  it('says what a document failed on where the word it explains is', async () => {
    /* Under the name, the server's sentence set the height of every row around
       it for something that matters on the one document in forty that failed. */
    docs = [doc({ id: 'd1', source: 'broken.pdf', status: 'failed', error: 'password-protected' })]
    await mount([base({ id: 'a' })])
    await open()

    const chip = document.querySelector('.knowledge-doc .knowledge-status') as HTMLElement
    expect(chip.textContent).toBe('Failed')
    expect(chip.getAttribute('title')).toBe('password-protected')
    expect(chip.classList.contains('knowledge-why')).toBe(true)
    /* And nowhere else: it is one sentence in one place. */
    expect(screen.queryByText('password-protected')).toBeNull()
  })

  it('carries a caveat the same way, on a document that did index', async () => {
    /* Same kind of sentence -- why the status is what it is -- and a document
       that indexed with half its figures unread says Ready either way. */
    docs = [doc({ id: 'd1', source: 'handbook.pdf', warning: '3 figures could not be read' })]
    await mount([base({ id: 'a' })])
    await open()

    const chip = document.querySelector('.knowledge-doc .knowledge-status') as HTMLElement
    expect(chip.textContent).toBe('Ready')
    expect(chip.getAttribute('title')).toBe('3 figures could not be read')
  })

  it('leaves a status with nothing to add unmarked', async () => {
    docs = [doc({ id: 'd1' })]
    await mount([base({ id: 'a' })])
    await open()

    const chip = document.querySelector('.knowledge-doc .knowledge-status') as HTMLElement
    expect(chip.getAttribute('title')).toBeNull()
    expect(chip.classList.contains('knowledge-why')).toBe(false)
  })

  it('swaps rows for cards, and back', async () => {
    docs = [{ id: 'd1', source: 'handbook.pdf', status: 'ready', chunk_count: 12, size: 2048, media_type: 'application/pdf', updated_at: '2026-09-24T14:55:00Z', folder_id: '', error: '', warning: '' }]
    await mount([base({ id: 'a' })])
    await act(async () => {
      ;(document.querySelector('.knowledge-card') as HTMLElement).click()
    })
    const toggle = (label: string): HTMLButtonElement =>
      document.querySelector(`.knowledge-views button[aria-label="${label}"]`) as HTMLButtonElement

    /* Rows to begin with: a document is read by its name, its size and when
       it was touched, and those are columns. */
    expect(document.querySelector('.knowledge-doc')).not.toBeNull()
    expect(toggle('List view').getAttribute('aria-pressed')).toBe('true')
    /* The columns are named, so four values read as columns. */
    expect(screen.getByText('Size')).toBeTruthy()
    expect(screen.getByText('Updated')).toBeTruthy()

    await act(async () => {
      toggle('Card view').click()
    })
    expect(document.querySelector('.knowledge-doc')).toBeNull()
    expect(docAt()).not.toBeUndefined()

    await act(async () => {
      toggle('List view').click()
    })
    expect(document.querySelector('.knowledge-doc')).not.toBeNull()
  })

  it('keeps the chosen view when the reader moves between bases', async () => {
    /* How someone reads is a fact about them, not about the base they opened. */
    await mount([base({ id: 'a' }), base({ id: 'b' })])
    await act(async () => {
      ;(document.querySelectorAll('.knowledge-card')[0] as HTMLElement).click()
    })
    await act(async () => {
      ;(document.querySelector('.knowledge-views button[aria-label="List view"]') as HTMLButtonElement).click()
    })

    await act(async () => {
      ;(document.querySelector('.knowledge-crumb') as HTMLButtonElement).click()
    })
    await act(async () => {
      ;(document.querySelectorAll('.knowledge-card')[1] as HTMLElement).click()
    })

    expect(store.get().view).toBe('list')
  })

  it('gives a document the drawing its format has', async () => {
    docs = [
      { id: 'd1', source: 'report.pdf', status: 'ready', chunk_count: 1, size: 10, media_type: 'application/pdf', updated_at: '', folder_id: '', error: '', warning: '' },
      { id: 'd2', source: 'notes.docx', status: 'ready', chunk_count: 1, size: 10, media_type: '', updated_at: '', folder_id: '', error: '', warning: '' },
      { id: 'd3', source: 'odd.thing', status: 'ready', chunk_count: 1, size: 10, media_type: '', updated_at: '', folder_id: '', error: '', warning: '' },
    ]
    await mount([base({ id: 'a' })])
    await act(async () => {
      ;(document.querySelector('.knowledge-card') as HTMLElement).click()
    })

    const drawn = [...document.querySelectorAll('img.knowledge-file')].map((n) =>
      (n.getAttribute('src') ?? '').replace(/\?.*$/, ''),
    )
    expect(drawn).toEqual(['assets/file-icon/pdf.svg', 'assets/file-icon/docx.svg'])
    /* The third has no drawing, so it falls back to the page with its own
       suffix written on it rather than to nothing. */
    expect([...document.querySelectorAll('.knowledge-filemark')].map((n) => n.textContent)).toEqual(['THIN'])
  })

  it('offers download, rebuild and delete on a document', async () => {
    /* Three rows, not the seven a fuller product offers: a menu row that
       cannot do what it says is worse than one that is not there. */
    docs = [{ id: 'd1', source: 'handbook.pdf', status: 'ready', chunk_count: 1, size: 10, media_type: 'application/pdf', updated_at: '', folder_id: '', error: '', warning: '' }]
    await mount([base({ id: 'a' })])
    await act(async () => {
      ;(document.querySelector('.knowledge-card') as HTMLElement).click()
    })

    await act(async () => {
      ;(docAt().querySelector('.knowledge-dots') as HTMLButtonElement).click()
    })

    expect(rows()).toEqual(['Download', 'Rebuild document', 'Move to folder', '-', 'Delete document'])
  })

  it('asks twice before deleting a document', async () => {
    docs = [{ id: 'd1', source: 'handbook.pdf', status: 'ready', chunk_count: 1, size: 10, media_type: 'application/pdf', updated_at: '', folder_id: '', error: '', warning: '' }]
    await mount([base({ id: 'a' })])
    await act(async () => {
      ;(document.querySelector('.knowledge-card') as HTMLElement).click()
    })
    await act(async () => {
      ;(docAt().querySelector('.knowledge-dots') as HTMLButtonElement).click()
    })

    await pick('Delete document')
    expect(removedDocs).toEqual([])

    await pick('Delete, really')
    expect(removedDocs).toEqual(['d1'])
    expect(docAt()).toBeUndefined()
  })

  it('opens the create sheet from the button above the grid', async () => {
    await mount([])

    await act(async () => {
      ;(document.querySelector('.knowledge-new') as HTMLButtonElement).click()
    })

    expect(document.querySelector('.knowledge-sheet')).not.toBeNull()
    /* Nothing to save until it is named. */
    const save = [...document.querySelectorAll('.knowledge-actions button')].at(-1) as HTMLButtonElement
    expect(save.disabled).toBe(true)
  })
  it('lists Root and the folders beside the documents', async () => {
    folders = [{ id: 'f1', name: 'specs', documents: 1 }]
    docs = [doc({ id: 'd1', source: 'handbook.pdf' }), doc({ id: 'd2', source: 'spec.pdf', folder_id: 'f1' })]
    await mount([base({ id: 'a' })])
    await open()

    const names = [...document.querySelectorAll('.knowledge-fname span')].map((n) => n.textContent)
    expect(names).toEqual(['Root', 'specs'])
    /* Root counts only what is in Root: it is not the base, it is the pile
       nobody filed. */
    expect([...document.querySelectorAll('.knowledge-fcount')].map((n) => n.textContent)).toEqual(['1', '1'])
    expect(screen.getByText('handbook.pdf')).toBeTruthy()
    expect(screen.queryByText('spec.pdf')).toBeNull()
  })

  it('shows a folder\'s own documents when it is picked', async () => {
    folders = [{ id: 'f1', name: 'specs', documents: 1 }]
    docs = [doc({ id: 'd1', source: 'handbook.pdf' }), doc({ id: 'd2', source: 'spec.pdf', folder_id: 'f1' })]
    await mount([base({ id: 'a' })])
    await open()

    await act(async () => {
      ;(document.querySelectorAll('.knowledge-fname')[1] as HTMLButtonElement).click()
    })

    expect(screen.getByText('spec.pdf')).toBeTruthy()
    expect(screen.queryByText('handbook.pdf')).toBeNull()
    /* And the path says which folder that is. */
    expect(document.querySelector('.knowledge-where .knowledge-here')?.textContent).toBe('specs')
  })

  it('names a new folder from the panel', async () => {
    await mount([base({ id: 'a' })])
    await open()

    await act(async () => {
      ;(document.querySelector('.knowledge-fadd') as HTMLButtonElement).click()
    })
    const field = document.querySelector('.knowledge-fnew') as HTMLInputElement
    await type(field, 'specs')
    await act(async () => {
      fireEvent.keyDown(field, { key: 'Enter' })
    })

    expect(folders.map((row) => row.name)).toEqual(['specs'])
  })

  it('hides the folder panel and brings it back', async () => {
    await mount([base({ id: 'a' })])
    await open()

    await act(async () => {
      ;(document.querySelector('.knowledge-fhide') as HTMLButtonElement).click()
    })
    expect(document.querySelector('.knowledge-tree')).toBeNull()

    await act(async () => {
      ;(document.querySelector('.knowledge-fshow') as HTMLButtonElement).click()
    })
    expect(document.querySelector('.knowledge-tree')).not.toBeNull()
  })

  it('offers Root and every folder when a document is moved', async () => {
    folders = [{ id: 'f1', name: 'specs', documents: 0 }]
    docs = [doc({ id: 'd1', source: 'handbook.pdf' })]
    await mount([base({ id: 'a' })])
    await open()

    await act(async () => {
      ;(docAt().querySelector('.knowledge-dots') as HTMLButtonElement).click()
    })
    await pick('Move to folder')

    const choices = [...document.querySelectorAll('.knowledge-movelist button span:first-child')]
    expect(choices.map((n) => n.textContent)).toEqual(['Root', 'specs'])
    /* Where it already is, ticked: a picker that does not say where you are
       makes the reader guess whether the move happened. */
    expect(document.querySelector('.knowledge-movelist .knowledge-cur span')?.textContent).toBe('Root')

    await act(async () => {
      ;(choices[1]!.parentElement as HTMLButtonElement).click()
    })
    expect(moved).toEqual([['d1', 'f1']])
    expect(document.querySelector('.knowledge-move')).toBeNull()
  })

  it('makes a folder and moves into it in one gesture', async () => {
    /* A reader who names a folder in the move sheet has said where this
       document goes; leaving it in Root would answer a different question. */
    docs = [doc({ id: 'd1', source: 'handbook.pdf' })]
    await mount([base({ id: 'a' })])
    await open()
    await act(async () => {
      ;(docAt().querySelector('.knowledge-dots') as HTMLButtonElement).click()
    })
    await pick('Move to folder')

    await act(async () => {
      ;(document.querySelector('.knowledge-movenew') as HTMLButtonElement).click()
    })
    const field = document.querySelector('.knowledge-move .knowledge-fnew') as HTMLInputElement
    await type(field, 'specs')
    await act(async () => {
      fireEvent.keyDown(field, { key: 'Enter' })
    })

    expect(folders.map((row) => row.name)).toEqual(['specs'])
    expect(moved).toEqual([['d1', 'f-specs']])
  })

  it('offers the four ways a document gets in', async () => {
    await mount([base({ id: 'a' })])
    await open()

    await act(async () => {
      ;(document.querySelector('.knowledge-add') as HTMLButtonElement).click()
    })

    expect(rows()).toEqual(['Upload document', 'Upload folder', 'Import from URL', 'Write a note'])
  })

  it('fetches a page the reader named', async () => {
    await mount([base({ id: 'a' })])
    await open()
    await act(async () => {
      ;(document.querySelector('.knowledge-add') as HTMLButtonElement).click()
    })
    await pick('Import from URL')

    await type(document.querySelector('.knowledge-sheet input') as HTMLInputElement, 'https://example.test/paper')
    await act(async () => {
      ;(document.querySelector('.knowledge-actions button:not(.ghost)') as HTMLButtonElement).click()
    })

    expect(added).toEqual(['https://example.test/paper'])
    expect(document.querySelector('.knowledge-sheet')).toBeNull()
  })
  it('opens a file over the page, with its pieces beside it', async () => {
    docs = [doc({ id: 'd1', source: 'handbook.pdf' })]
    chunks = [chunk({ chunk_index: 0, text: 'the first piece' }), chunk({ chunk_index: 1, text: 'the second' })]
    await mount([base({ id: 'a' })])
    await open()

    await act(async () => {
      docAt().click()
    })

    /* The list is gone: the widest thing on screen is the thing being read. */
    expect(docAt()).toBeUndefined()
    expect(document.querySelector('.knowledge-frame')?.getAttribute('src')).toContain('document=d1')
    expect([...document.querySelectorAll('.knowledge-chunktx')].map((n) => n.textContent)).toEqual([
      'the first piece',
      'the second',
    ])
    /* Numbered from 1, the way a reader counts. */
    expect([...document.querySelectorAll('.knowledge-chunkix')].map((n) => n.textContent)).toEqual(['#1', '#2'])
  })

  it('says what the parser knew about a piece and nothing it did not', async () => {
    /* An absent page means the format has none, never that it is page zero. */
    docs = [doc({ id: 'd1', source: 'handbook.pdf' })]
    chunks = [
      chunk({ chunk_index: 0, layout_type: 'table', page_number: 3, heading_path: ['Ops', 'Backups'] }),
      chunk({ chunk_index: 1 }),
    ]
    await mount([base({ id: 'a' })])
    await open()
    await act(async () => {
      docAt().click()
    })

    const first = document.querySelectorAll('.knowledge-chunk')[0]!
    expect(first.querySelector('.knowledge-chunkty')?.textContent).toBe('table')
    expect(first.querySelector('.knowledge-chunkpg')?.textContent).toBe('p. 3')
    expect(first.querySelector('.knowledge-chunkpath')?.textContent).toBe('Ops > Backups')
    const second = document.querySelectorAll('.knowledge-chunk')[1]!
    expect(second.querySelector('.knowledge-chunkpg')).toBeNull()
    expect(second.querySelector('.knowledge-chunkty')).toBeNull()
  })

  it('shows the region of the page a piece was cut from, where one was stored', async () => {
    docs = [doc({ id: 'd1', source: 'handbook.pdf' })]
    chunks = [chunk({ chunk_index: 0, has_crop: true }), chunk({ chunk_index: 1 })]
    await mount([base({ id: 'a' })])
    await open()
    await act(async () => {
      docAt().click()
    })

    const crops = [...document.querySelectorAll('.knowledge-crop')]
    expect(crops).toHaveLength(1)
    expect(crops[0]!.getAttribute('src')).toBe('/knowledge/crop?document=d1&chunk=c0')
    /* Lazy: a document of two hundred pieces is two hundred pictures, and a
       reader reads a few of them. */
    expect(crops[0]!.getAttribute('loading')).toBe('lazy')
  })

  it('says a document has no pieces rather than showing an empty column', async () => {
    /* One that failed, one still queued and one in a base with no model all
       land here, and saying so beats a blank a reader has to interpret. */
    docs = [doc({ id: 'd1', source: 'handbook.pdf', status: 'failed' })]
    await mount([base({ id: 'a' })])
    await open()
    await act(async () => {
      docAt().click()
    })

    expect(screen.getByText('Nothing indexed yet')).toBeTruthy()
  })

  it('offers a download for a format it cannot draw', async () => {
    docs = [doc({ id: 'd1', source: 'odd.thing' })]
    await mount([base({ id: 'a' })])
    await open()
    await act(async () => {
      docAt().click()
    })

    expect(document.querySelector('.knowledge-frame')).toBeNull()
    expect(screen.getByText('This format cannot be shown here')).toBeTruthy()
    expect(document.querySelector('a[download]')?.getAttribute('download')).toBe('odd.thing')
  })

  it('goes back to the list, by the path and by Escape', async () => {
    docs = [doc({ id: 'd1', source: 'handbook.pdf' })]
    await mount([base({ id: 'a' })])
    await open()
    await act(async () => {
      docAt().click()
    })

    await act(async () => {
      ;(document.querySelector('.knowledge-crumb') as HTMLButtonElement).click()
    })
    expect(store.get().viewing).toBeNull()
    expect(docAt()).not.toBeUndefined()

    await act(async () => {
      docAt().click()
    })
    await act(async () => {
      fireEvent.keyDown(document, { key: 'Escape' })
    })

    /* Out of the file, not out of the page: the base is still open behind it. */
    expect(store.get().viewing).toBeNull()
    expect(store.get().opened?.id).toBe('a')
  })

  it('does not open the file when the dots were what was pressed', async () => {
    docs = [doc({ id: 'd1', source: 'handbook.pdf' })]
    await mount([base({ id: 'a' })])
    await open()

    await act(async () => {
      ;(docAt().querySelector('.knowledge-dots') as HTMLButtonElement).click()
    })

    expect(store.get().viewing).toBeNull()
  })
  it('offers the toolbar the chunk list is worked with', async () => {
    docs = [doc({ id: 'd1', source: 'handbook.pdf' })]
    chunks = [chunk({ chunk_index: 0 }), chunk({ chunk_index: 1 })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()

    expect(screen.getByText('Total 2')).toBeTruthy()
    expect(screen.getByText('Select all')).toBeTruthy()
    expect(document.querySelector('.knowledge-chunkq')).not.toBeNull()
    expect(document.querySelector('.knowledge-chunkfilter')).not.toBeNull()
    expect(document.querySelector('.knowledge-chunks .knowledge-add')).not.toBeNull()
  })

  it('clips a piece until the reader asks for all of it', async () => {
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0 })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()

    expect(document.querySelector('.knowledge-chunktx')?.classList.contains('knowledge-clip')).toBe(true)

    await act(async () => {
      ;(screen.getByText('Full text') as HTMLButtonElement).click()
    })

    expect(document.querySelector('.knowledge-chunktx')?.classList.contains('knowledge-clip')).toBe(false)
  })

  it('narrows the list as the reader types, once rather than per key', async () => {
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0, text: 'backups run nightly' }), chunk({ chunk_index: 1, text: 'on call rota' })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()
    const reads = asked.length

    const field = document.querySelector('.knowledge-chunkq') as HTMLInputElement
    await type(field, 'back')
    await type(field, 'backups')

    await vi.waitFor(() => expect(store.get().chunks).toHaveLength(1))
    expect(asked.slice(reads).map((ask) => ask.query)).toEqual(['backups'])
    expect(screen.getByText('backups run nightly')).toBeTruthy()
  })

  it('offers the three states a piece can be filtered to', async () => {
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0 }), chunk({ chunk_index: 1, enabled: false })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()

    await act(async () => {
      ;(document.querySelector('.knowledge-chunkfilter') as HTMLButtonElement).click()
    })
    expect(rows()).toEqual(['All', 'Enabled', 'Disabled'])

    await pick('Disabled')
    await vi.waitFor(() => expect(store.get().chunks).toHaveLength(1))

    expect(store.get().chunks!.map((row) => row.chunk_index)).toEqual([1])
    /* And the control says it is narrowing, so an empty list under it does
       not read as a document with nothing in it. */
    expect(document.querySelector('.knowledge-chunkfilter')?.classList.contains('knowledge-cur')).toBe(true)
  })

  it('turns one piece off from its own row', async () => {
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0 })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()

    const toggle = document.querySelector('.knowledge-chunkon') as HTMLButtonElement
    expect(toggle.getAttribute('aria-checked')).toBe('true')
    await act(async () => {
      toggle.click()
    })

    expect(switched).toEqual([[['c0'], false]])
    expect(document.querySelector('.knowledge-chunk')?.classList.contains('knowledge-off')).toBe(true)
  })

  it('acts on everything ticked at once', async () => {
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0 }), chunk({ chunk_index: 1 })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()

    await act(async () => {
      ;(document.querySelector('.knowledge-chunkall input') as HTMLInputElement).click()
    })
    /* The count replaces the label: a bar showing both leaves a reader
       working out which number the buttons apply to. */
    expect(screen.getByText('2 selected')).toBeTruthy()

    await act(async () => {
      ;(screen.getByText('Disable') as HTMLButtonElement).click()
    })

    expect(switched).toEqual([[['c0', 'c1'], false]])
  })

  it('leaves a piece with no id out of the batch and out of reach', async () => {
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0 }), chunk({ chunk_index: 1, chunk_id: '' })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()

    const ticks = [...document.querySelectorAll('.knowledge-chunkhd input')] as HTMLInputElement[]
    expect(ticks.map((box) => box.disabled)).toEqual([false, true])
    const toggles = [...document.querySelectorAll('.knowledge-chunkon')] as HTMLButtonElement[]
    expect(toggles.map((box) => box.disabled)).toEqual([false, true])

    await act(async () => {
      ;(document.querySelector('.knowledge-chunkall input') as HTMLInputElement).click()
    })
    expect(store.get().picked).toEqual(['c0'])
  })

  it('asks twice before taking a piece out of the index', async () => {
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0 })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()

    await act(async () => {
      ;(document.querySelector('.knowledge-chunk .knowledge-dots') as HTMLButtonElement).click()
    })
    expect(rows()).toEqual(['Edit', 'Disable', '-', 'Delete'])

    await pick('Delete')
    expect(killed).toEqual([])
    expect(rows()).toEqual(['Edit', 'Disable', '-', 'Delete, really'])

    await pick('Delete, really')
    expect(killed).toEqual([['c0']])
  })

  it('rewrites one piece where it sits', async () => {
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0, text: 'as parsed' })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()

    await act(async () => {
      ;(document.querySelector('.knowledge-chunk .knowledge-dots') as HTMLButtonElement).click()
    })
    await pick('Edit')

    const box = document.querySelector('.knowledge-chunkedit textarea') as HTMLTextAreaElement
    expect(box.value).toBe('as parsed')
    await type(box as unknown as HTMLInputElement, 'said differently')
    await act(async () => {
      ;(document.querySelector('.knowledge-chunkedit button:not(.ghost)') as HTMLButtonElement).click()
    })

    expect(screen.getByText('said differently')).toBeTruthy()
    expect(document.querySelector('.knowledge-chunkedit')).toBeNull()
  })

  it('writes a piece of its own at the top of the list', async () => {
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0 })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()

    await act(async () => {
      ;(document.querySelector('.knowledge-chunks .knowledge-add') as HTMLButtonElement).click()
    })
    const box = document.querySelector('.knowledge-chunkedit textarea') as HTMLTextAreaElement
    await type(box as unknown as HTMLInputElement, 'a paragraph nobody parsed')
    await act(async () => {
      ;(document.querySelector('.knowledge-chunkedit button:not(.ghost)') as HTMLButtonElement).click()
    })

    /* Appended, not inserted: where a written piece belongs in the reading
       order is a question the document cannot answer. */
    expect(store.get().chunks!.map((row) => row.text)).toEqual(['piece 0', 'a paragraph nobody parsed'])
    expect(document.querySelector('.knowledge-chunkedit')).toBeNull()
  })

  it('says a filter matched nothing rather than that the document has nothing', async () => {
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0, text: 'backups run nightly' })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()

    await type(document.querySelector('.knowledge-chunkq') as HTMLInputElement, 'rota')
    await vi.waitFor(() => expect(store.get().chunks).toEqual([]))

    expect(screen.getByText('Nothing matched')).toBeTruthy()
    expect(screen.queryByText('Nothing indexed yet')).toBeNull()
  })

  it('pages the list only when there is more than one page of it', async () => {
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0 })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()
    expect(document.querySelector('.knowledge-pager')).toBeNull()

    await act(async () => {
      store.closeDoc()
    })
    chunks = Array.from({ length: store.CHUNK_PAGE + 3 }, (_, at) => chunk({ chunk_index: at }))
    await openDoc()

    expect(document.querySelector('.knowledge-pager')).not.toBeNull()
    expect(screen.getByText(`1-${store.CHUNK_PAGE} of ${store.CHUNK_PAGE + 3}`)).toBeTruthy()
    await act(async () => {
      ;(document.querySelector('[aria-label="Next page"]') as HTMLButtonElement).click()
    })
    await vi.waitFor(() => expect(store.get().chunks).toHaveLength(3))
  })
  it('takes the file to where a piece was cut from when it is clicked', async () => {
    docs = [doc({ id: 'd1', source: 'handbook.pdf' })]
    chunks = [
      chunk({ chunk_index: 0 }),
      chunk({ chunk_index: 1, regions: [{ page_number: 4, x0: 72, top: 318.4, x1: 523, bottom: 460 }] }),
    ]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()

    await act(async () => {
      ;(document.querySelectorAll('.knowledge-chunk')[1] as HTMLElement).click()
    })

    /* The PDF open-parameters set, which the browser's own viewer reads: the
       page, and the point to put at the top of the frame. */
    const frame = document.querySelector('.knowledge-frame')!
    expect(frame.getAttribute('src')).toContain('#page=4&view=FitH,318')
    /* And the row says it is the one the file is open at. */
    expect(document.querySelectorAll('.knowledge-chunk')[1]!.classList.contains('knowledge-cur')).toBe(true)

    /* A different frame, not the same one re-pointed: the viewer reads the
       fragment when it loads the document and never again, so moving it means
       loading it again. */
    await act(async () => {
      ;(document.querySelectorAll('.knowledge-chunk')[0] as HTMLElement).click()
    })
    expect(document.querySelector('.knowledge-frame')).not.toBe(frame)
  })

  it('goes back to the same place when the same piece is pressed again', async () => {
    /* A reader who has scrolled away and pressed it again means to be taken
       back, and the frame only reads where to go when it loads. */
    docs = [doc({ id: 'd1', source: 'deck.pptx' })]
    chunks = [chunk({ chunk_index: 0, regions: [{ page_number: 3, x0: 0, top: 96, x1: 500, bottom: 300 }] })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()

    await act(async () => {
      ;(document.querySelector('.knowledge-chunk') as HTMLElement).click()
    })
    const first = document.querySelector('.knowledge-frame')!
    /* A slide is a page: the gateway renders the deck to a PDF on the way out,
       and the parser numbered the slides. */
    expect(first.getAttribute('src')).toContain('render=pdf')
    expect(first.getAttribute('src')).toContain('#page=3&view=FitH,96')

    await act(async () => {
      ;(document.querySelector('.knowledge-chunk') as HTMLElement).click()
    })

    expect(document.querySelector('.knowledge-frame')).not.toBe(first)
  })

  it('leaves the file where it is for a piece with no place on a page', async () => {
    /* A text file has no pages, so there is nowhere to scroll to and saying
       otherwise would move the preview for no reason. */
    docs = [doc({ id: 'd1', source: 'notes.txt' })]
    chunks = [chunk({ chunk_index: 0 })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()

    await act(async () => {
      ;(document.querySelector('.knowledge-chunk') as HTMLElement).click()
    })

    expect(document.querySelector('.knowledge-frame')?.getAttribute('src')).not.toContain('#page=')
    /* The row is still marked: it is the one being read. */
    expect(document.querySelector('.knowledge-chunk')?.classList.contains('knowledge-cur')).toBe(true)
  })

  it('opens a window to rewrite a piece when it is double-clicked', async () => {
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0, text: 'as parsed' })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()

    await act(async () => {
      fireEvent.doubleClick(document.querySelector('.knowledge-chunk') as HTMLElement)
    })

    const box = document.querySelector('.knowledge-chunksheet textarea') as HTMLTextAreaElement
    expect(box.value).toBe('as parsed')
    await type(box as unknown as HTMLInputElement, 'said differently')
    await act(async () => {
      ;(document.querySelector('.knowledge-chunksheet button:not(.ghost)') as HTMLButtonElement).click()
    })

    expect(screen.getByText('said differently')).toBeTruthy()
    expect(document.querySelector('.knowledge-chunksheet')).toBeNull()
  })

  it('shuts the editor on Escape before it shuts the file', async () => {
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0 })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()
    await act(async () => {
      fireEvent.doubleClick(document.querySelector('.knowledge-chunk') as HTMLElement)
    })

    await act(async () => {
      fireEvent.keyDown(document, { key: 'Escape' })
    })
    expect(document.querySelector('.knowledge-chunksheet')).toBeNull()
    expect(store.get().viewing?.id).toBe('d1')

    await act(async () => {
      fireEvent.keyDown(document, { key: 'Escape' })
    })
    expect(store.get().viewing).toBeNull()
  })

  it('does not take the file anywhere when a row control was what was pressed', async () => {
    docs = [doc({ id: 'd1', source: 'handbook.pdf' })]
    chunks = [chunk({ chunk_index: 0, regions: [{ page_number: 2, x0: 0, top: 10, x1: 10, bottom: 20 }] })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()

    await act(async () => {
      ;(document.querySelector('.knowledge-chunkhd input') as HTMLInputElement).click()
    })
    expect(store.get().aimed).toBe('')

    await act(async () => {
      ;(document.querySelector('.knowledge-chunkon') as HTMLButtonElement).click()
    })
    expect(store.get().aimed).toBe('')
  })

  it('shows a crop small, and the whole of it on hover', async () => {
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0, has_crop: true })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()

    expect(document.querySelector('.knowledge-cropbig')).toBeNull()

    await act(async () => {
      fireEvent.mouseEnter(document.querySelector('.knowledge-cropwrap') as HTMLElement)
    })
    const big = document.querySelector('.knowledge-cropbig')
    expect(big).not.toBeNull()
    expect(big!.querySelector('img')?.getAttribute('src')).toBe('/knowledge/crop?document=d1&chunk=c0')

    await act(async () => {
      fireEvent.mouseLeave(document.querySelector('.knowledge-cropwrap') as HTMLElement)
    })
    expect(document.querySelector('.knowledge-cropbig')).toBeNull()
  })
  it('draws the file as its own pages, not as a frame', async () => {
    docs = [doc({ id: 'd1', source: 'handbook.pdf' })]
    sheets = [
      { number: 1, width: 595, height: 842 },
      { number: 2, width: 595, height: 842 },
    ]
    chunks = [chunk({ chunk_index: 0 })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()
    await vi.waitFor(() => expect(store.get().pages).toHaveLength(2))

    expect(document.querySelector('.knowledge-frame')).toBeNull()
    const drawn = [...document.querySelectorAll('.knowledge-sheet2 img')].map((n) => n.getAttribute('src'))
    expect(drawn).toEqual(['/knowledge/page?document=d1&page=1', '/knowledge/page?document=d1&page=2'])
    /* Lazy, and shaped before the picture arrives so the column has its full
       height from the start. */
    expect(document.querySelector('.knowledge-sheet2 img')?.getAttribute('loading')).toBe('lazy')
    expect((document.querySelector('.knowledge-sheet2') as HTMLElement).style.aspectRatio).toBe('595 / 842')
  })

  it('covers the region a piece was cut from, on its own page', async () => {
    docs = [doc({ id: 'd1', source: 'handbook.pdf' })]
    sheets = [
      { number: 1, width: 600, height: 800 },
      { number: 2, width: 600, height: 800 },
    ]
    chunks = [
      chunk({ chunk_index: 0 }),
      chunk({
        chunk_index: 1,
        regions: [{ page_number: 2, x0: 60, top: 200, x1: 540, bottom: 400 }],
      }),
    ]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()
    await vi.waitFor(() => expect(store.get().pages).toHaveLength(2))

    /* Nothing is covered until a piece is being read. */
    expect(document.querySelectorAll('.knowledge-mark')).toHaveLength(0)

    await act(async () => {
      ;(document.querySelectorAll('.knowledge-chunk')[1] as HTMLElement).click()
    })

    const marks = [...document.querySelectorAll('.knowledge-mark')] as HTMLElement[]
    expect(marks).toHaveLength(1)
    /* On page two, and nowhere on page one. */
    expect(marks[0]!.closest('.knowledge-sheet2')?.getAttribute('data-page')).toBe('2')
    /* In fractions of the page: 60/600 across, 200/800 down, 480 wide of 600,
       200 tall of 800. */
    expect(marks[0]!.style.left).toBe('10%')
    expect(marks[0]!.style.top).toBe('25%')
    expect(marks[0]!.style.width).toBe('80%')
    expect(marks[0]!.style.height).toBe('25%')
  })

  it('covers every page a piece that crossed a boundary was cut from', async () => {
    docs = [doc({ id: 'd1', source: 'handbook.pdf' })]
    sheets = [
      { number: 1, width: 600, height: 800 },
      { number: 2, width: 600, height: 800 },
    ]
    chunks = [
      chunk({
        chunk_index: 0,
        regions: [
          { page_number: 1, x0: 60, top: 600, x1: 540, bottom: 780 },
          { page_number: 2, x0: 60, top: 40, x1: 540, bottom: 200 },
        ],
      }),
    ]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()
    await vi.waitFor(() => expect(store.get().pages).toHaveLength(2))

    await act(async () => {
      ;(document.querySelector('.knowledge-chunk') as HTMLElement).click()
    })

    const on = [...document.querySelectorAll('.knowledge-mark')].map(
      (n) => n.closest('.knowledge-sheet2')?.getAttribute('data-page'),
    )
    expect(on).toEqual(['1', '2'])
  })

  it('frames a file that has no pages', async () => {
    /* A text file, a note, a format nothing can render. Not a failure: it is
       the panel doing what it did for every format before pages. */
    docs = [doc({ id: 'd1', source: 'notes.txt' })]
    chunks = [chunk({ chunk_index: 0 })]
    await mount([base({ id: 'a' })])
    await open()
    await openDoc()
    await vi.waitFor(() => expect(store.get().pages).toEqual([]))

    expect(document.querySelector('.knowledge-pages')).toBeNull()
    expect(document.querySelector('.knowledge-frame')).not.toBeNull()
  })
  it('opens how the base is tuned from the documents page', async () => {
    await mount([base({ id: 'a', embedding_model: 'bge-m3', top_k: 9, chunk_size: 1024, chunk_overlap: 100 })])
    await open()

    await act(async () => {
      ;(document.querySelector('.knowledge-gear') as HTMLButtonElement).click()
    })

    const panel = document.querySelector('.knowledge-sets')!
    expect(panel).not.toBeNull()
    /* On the model it was built on. */
    expect((panel.querySelector('select[aria-label="Embedding model"]') as HTMLSelectElement).value).toBe('bge-m3')
    expect((panel.querySelector('input[type="range"]') as HTMLInputElement).value).toBe('9')
    expect((panel.querySelector('input[aria-label="Piece size"]') as HTMLInputElement).value).toBe('1024')
  })

  it('writes what the reader tuned, and nothing it was not asked about', async () => {
    await mount([base({ id: 'a' })])
    await open()
    await act(async () => {
      ;(document.querySelector('.knowledge-gear') as HTMLButtonElement).click()
    })

    await type(document.querySelector('input[aria-label="Piece size"]') as HTMLInputElement, '512')
    await act(async () => {
      ;(screen.getByText('Save') as HTMLButtonElement).click()
    })

    expect(wrote).toHaveLength(1)
    const [id, settings] = wrote[0]!
    expect(id).toBe('a')
    expect(settings.chunk_size).toBe(512)
    /* The name is not among them: this panel does not rename anything. */
    expect(settings.name).toBeUndefined()
    expect(document.querySelector('.knowledge-sets')).toBeNull()
  })

  it('refuses an overlap that is not smaller than the piece', async () => {
    /* An overlap as big as the piece would make every piece the one before. */
    await mount([base({ id: 'a', chunk_size: 512 })])
    await open()
    await act(async () => {
      ;(document.querySelector('.knowledge-gear') as HTMLButtonElement).click()
    })

    await type(document.querySelector('input[aria-label="Overlap"]') as HTMLInputElement, '900')

    expect(screen.getByText('Overlap must be less than the piece size')).toBeTruthy()
    expect((screen.getByText('Save') as HTMLButtonElement).disabled).toBe(true)
  })

  it('puts every tuning field back with one press', async () => {
    /* A reader who has moved four fields needs one way out, not four. */
    await mount([base({ id: 'a', top_k: 40, chunk_size: 512, chunk_overlap: 10 })])
    await open()
    await act(async () => {
      ;(document.querySelector('.knowledge-gear') as HTMLButtonElement).click()
    })

    await act(async () => {
      ;(screen.getByText('Restore defaults') as HTMLButtonElement).click()
    })

    expect((document.querySelector('input[type="range"]') as HTMLInputElement).value).toBe('6')
    expect((document.querySelector('input[aria-label="Piece size"]') as HTMLInputElement).value).toBe('2048')
    expect((document.querySelector('input[aria-label="Overlap"]') as HTMLInputElement).value).toBe('215')
    expect((document.querySelector('input[aria-label="Table context"]') as HTMLInputElement).value).toBe('64')
    expect((document.querySelector('input[aria-label="Figure context"]') as HTMLInputElement).value).toBe('64')
  })

  it('offers the separator only when the chunker is not cutting on structure', async () => {
    await mount([base({ id: 'a', smart_chunking: true })])
    await open()
    await act(async () => {
      ;(document.querySelector('.knowledge-gear') as HTMLButtonElement).click()
    })

    const sep = (): HTMLInputElement => document.querySelector('input[aria-label="Separator"]') as HTMLInputElement
    expect(sep().disabled).toBe(true)
    /* And written as it is typed: the separator a reader means is two
       newlines, which a one-line field cannot hold as themselves. */
    expect(sep().value).toBe('\\n\\n')

    await act(async () => {
      ;(document.querySelector('.knowledge-sets .knowledge-chunkon') as HTMLButtonElement).click()
    })
    expect(sep().disabled).toBe(false)
  })
  it('carries prose into a table or a figure by as much as the reader said', async () => {
    /* A table on its own embeds as a grid of values with nothing saying what
       they are about, and a figure is a caption and nothing else. */
    await mount([base({ id: 'a', table_context_size: 96, image_context_size: 32 })])
    await open()
    await act(async () => {
      ;(document.querySelector('.knowledge-gear') as HTMLButtonElement).click()
    })

    const field = (label: string): HTMLInputElement =>
      document.querySelector(`input[aria-label="${label}"]`) as HTMLInputElement
    expect(field('Table context').value).toBe('96')
    expect(field('Figure context').value).toBe('32')

    await type(field('Table context'), '0')
    await act(async () => {
      ;(screen.getByText('Save') as HTMLButtonElement).click()
    })

    /* Zero is a setting, not an empty field: it carries no prose at all. */
    expect(wrote[0]![1].table_context_size).toBe(0)
    expect(wrote[0]![1].image_context_size).toBe(32)
  })

  it('refuses a context that is not a count', async () => {
    await mount([base({ id: 'a' })])
    await open()
    await act(async () => {
      ;(document.querySelector('.knowledge-gear') as HTMLButtonElement).click()
    })

    await type(document.querySelector('input[aria-label="Figure context"]') as HTMLInputElement, '-5')

    expect((screen.getByText('Save') as HTMLButtonElement).disabled).toBe(true)
  })
  it('offers the embedding model while the base holds no pieces', async () => {
    /* Nothing indexed is nothing to lose: the collection has no vectors in it
       and no document has been cut, so the model is a choice. */
    await mount([base({ id: 'a', chunks: 0, embedding_model: 'bge-m3' })])
    await open()
    await act(async () => {
      ;(document.querySelector('.knowledge-gear') as HTMLButtonElement).click()
    })

    const pick = document.querySelector('select[aria-label="Embedding model"]') as HTMLSelectElement
    expect(pick).not.toBeNull()
    /* Every embedding model the install can reach, the way the settings page's
       own Embedding row lists them -- and off, first, because it is the one
       choice that is not a model. */
    expect([...pick.options].map((o) => o.value)).toEqual(['', 'bge-m3', 'text-embedding-3-small'])
    /* Grouped by who serves them: the same id under two accounts is two
       endpoints, and a flat list would make the reader guess which. */
    expect([...pick.querySelectorAll('optgroup')].map((g) => g.label)).toEqual(['Ollama', 'OpenAI'])
    expect(screen.getByText(/Nothing is indexed yet/)).toBeTruthy()
  })

  it('states the embedding model once the base holds pieces', async () => {
    /* Moving it then is the collection made again at the new width and every
       document re-cut -- a rebuild, not something a panel does by saving. */
    await mount([base({ id: 'a', chunks: 41, embedding_model: 'bge-m3' })])
    await open()
    await act(async () => {
      ;(document.querySelector('.knowledge-gear') as HTMLButtonElement).click()
    })

    expect(document.querySelector('select[aria-label="Embedding model"]')).toBeNull()
    expect(document.querySelector('.knowledge-fixed')?.textContent).toBe('bge-m3')
  })

  it('counts pieces rather than files when it decides that', async () => {
    /* A base whose uploads all failed holds nothing, whatever the file count
       says, and there is still nothing to throw away. */
    await mount([base({ id: 'a', documents: 7, chunks: 0 })])
    await open()
    await act(async () => {
      ;(document.querySelector('.knowledge-gear') as HTMLButtonElement).click()
    })

    expect(document.querySelector('select[aria-label="Embedding model"]')).not.toBeNull()
  })

  it('sends the model with the account it is reached through, and only when it moved', async () => {
    await mount([base({ id: 'a', chunks: 0, embedding_model: '' })])
    await open()
    await act(async () => {
      ;(document.querySelector('.knowledge-gear') as HTMLButtonElement).click()
    })

    /* Saved untouched: the key is a rebuild, and writing the same model back
       would queue every document for nothing. */
    await act(async () => {
      ;(screen.getByText('Save') as HTMLButtonElement).click()
    })
    expect(wrote[0]![1].embedding_model).toBeUndefined()

    await act(async () => {
      ;(document.querySelector('.knowledge-gear') as HTMLButtonElement).click()
    })
    const pick = document.querySelector('select[aria-label="Embedding model"]') as HTMLSelectElement
    await act(async () => {
      fireEvent.change(pick, { target: { value: 'bge-m3' } })
    })
    await act(async () => {
      ;(screen.getByText('Save') as HTMLButtonElement).click()
    })

    /* A model id names no credential, so the account it was picked under
       travels with it. */
    expect(wrote[1]![1].embedding_model).toBe('bge-m3')
    expect(wrote[1]![1].embedding_provider).toBe('ollama')
  })

  it('offers turning embedding off, which is storing without indexing', async () => {
    await mount([base({ id: 'a', chunks: 0, embedding_model: 'bge-m3' })])
    await open()
    await act(async () => {
      ;(document.querySelector('.knowledge-gear') as HTMLButtonElement).click()
    })

    const pick = document.querySelector('select[aria-label="Embedding model"]') as HTMLSelectElement
    await act(async () => {
      fireEvent.change(pick, { target: { value: '' } })
    })
    await act(async () => {
      ;(screen.getByText('Save') as HTMLButtonElement).click()
    })

    expect(wrote[0]![1].embedding_model).toBe('')
  })
  it('keeps offering a model the catalogue no longer carries', async () => {
    /* A base built on a model since removed still has to be able to say what
       it is on, and a picker that dropped it would silently offer to move the
       base off it. */
    await mount([base({ id: 'a', chunks: 0, embedding_model: 'retired-embed' })])
    await open()
    await act(async () => {
      ;(document.querySelector('.knowledge-gear') as HTMLButtonElement).click()
    })

    const pick = document.querySelector('select[aria-label="Embedding model"]') as HTMLSelectElement
    expect(pick.value).toBe('retired-embed')
    expect([...pick.options].map((o) => o.value)).toContain('retired-embed')
  })

  it('reads the catalogue only for a base whose model could still move', async () => {
    /* A base that holds pieces cannot be moved, so reading every provider's
       models for it is a call whose answer the panel may not offer. */
    await mount([base({ id: 'a', chunks: 12 })])
    await open()
    const reads = vi.spyOn(sources.knowledge!, 'models')

    await act(async () => {
      ;(document.querySelector('.knowledge-gear') as HTMLButtonElement).click()
    })

    expect(reads).not.toHaveBeenCalled()
  })

  it('opens the panel even where the catalogue could not be read', async () => {
    /* The model it is on and nothing to change it to beats a panel that will
       not open: every other setting is still readable. */
    install([base({ id: 'a', chunks: 0, embedding_model: '' })])
    sources.knowledge!.models = async () => {
      throw new Error('no providers configured')
    }
    const host = document.createElement('div')
    host.id = 'menu'
    document.body.appendChild(host)
    render(<KnowledgeApp />)
    await act(async () => {
      await store.load()
    })
    await open()
    await act(async () => {
      ;(document.querySelector('.knowledge-gear') as HTMLButtonElement).click()
    })

    expect(document.querySelector('.knowledge-sets')).not.toBeNull()
    const pick = document.querySelector('select[aria-label="Embedding model"]') as HTMLSelectElement
    expect([...pick.options].map((o) => o.value)).toEqual([''])
  })
})
