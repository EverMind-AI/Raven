// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSources, sources } from '../../state/sources'
import * as store from './store'

import type { KbBase, KbChunk, KbChunkAsk, KbModel, KbPage, KbDoc, KbFolder, KbSettings } from './types'

/* The knowledge page's state, against a source that answers from memory.
 *
 * What these cover is the two things the page decides for itself: which rows a
 * tab shows and in what order, and that a write puts the server's answer back
 * rather than guessing what it did.
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

interface Fake {
  rows: KbBase[]
  wrote: Array<[string, KbSettings]>
  removed: string[]
  fail?: string
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
let chunksFail = ''
const removedDocs: string[] = []
/** Every add, in the order it was made, as the seam saw it. */
const added: string[] = []
/** Every document the store asked to have cut and embedded. */
const indexed: string[] = []
let indexFails = ''

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

/** Into the base, then into its first document, with the chunks read. */
async function openFirstDoc(): Promise<void> {
  store.openBase(store.get().bases![0]!)
  await vi.waitFor(() => expect(store.get().documents).not.toBeNull())
  store.openDoc(store.get().documents![0]!)
  await vi.waitFor(() => expect(store.get().chunks).not.toBeNull())
}

/** A row for a document that has just arrived, as the server would answer. */
function arrived(source: string): KbDoc {
  const made = doc({ id: `d-${source}`, source, status: 'pending' })
  docs = [...docs, made]
  added.push(source)
  return made
}

function install(rows: KbBase[]): Fake {
  const fake: Fake = { rows, wrote: [], removed: [] }
  sources.knowledge = {
    bases: async () => {
      if (fake.fail) throw new Error(fake.fail)
      return fake.rows
    },
    documents: async () => docs,
    reindex: async (id) => {
      indexed.push(id)
      if (indexFails) throw new Error(indexFails)
      docs = docs.map((row) => (row.id === id ? { ...row, status: 'ready' } : row))
      return docs.find((row) => row.id === id)!
    },
    removeDoc: async (id) => {
      removedDocs.push(id)
    },
    models: async () => offers,
    pages: async () => sheets,
    chunks: async (_id, ask) => {
      if (chunksFail) throw new Error(chunksFail)
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
    /* As the server does it: the folder goes, its documents come back to
       Root rather than going with it. */
    removeFolder: async (id) => {
      folders = folders.filter((row) => row.id !== id)
      docs = docs.map((row) => (row.folder_id === id ? { ...row, folder_id: '' } : row))
    },
    move: async (id, folderId) => {
      docs = docs.map((row) => (row.id === id ? { ...row, folder_id: folderId } : row))
      return docs.find((row) => row.id === id)!
    },
    upload: async (_baseId, name) => arrived(name),
    addUrl: async (_baseId, url) => arrived(url),
    addNote: async (_baseId, title) => arrived(title),
    create: async (name, description) => base({ id: name, name, description }),
    settings: async (id, settings) => {
      fake.wrote.push([id, settings])
      const found = fake.rows.find((row) => row.id === id)!
      return { ...found, ...settings } as KbBase
    },
    remove: async (id) => {
      fake.removed.push(id)
    },
  }
  return fake
}

beforeEach(() => {
  store._resetForTests()
  docs = []
  folders = []
  chunks = []
  sheets = []
  asked.length = 0
  switched.length = 0
  killed.length = 0
  chunksFail = ''
  added.length = 0
  indexed.length = 0
  indexFails = ''
  removedDocs.length = 0
})

afterEach(() => {
  resetSources()
  vi.restoreAllMocks()
})

describe('the knowledge store', () => {
  it('reads nothing until it has read', async () => {
    /* Null is not the empty list: one is "wait", the other is "there is
       nothing here", and a page that showed the same for both would be lying
       half the time. */
    install([])
    expect(store.get().bases).toBeNull()

    await store.load()

    expect(store.get().bases).toEqual([])
  })

  it('keeps the reason the list could not be read', async () => {
    const fake = install([])
    fake.fail = 'no gateway'

    await store.load()

    expect(store.get().failed).toBe('no gateway')
    /* And an empty list behind it, so the grid draws nothing rather than
       staying on the loading line for ever. */
    expect(store.get().bases).toEqual([])
  })

  it('shows every base on the all tab, by name', async () => {
    install([base({ id: 'beta' }), base({ id: 'alpha' })])
    await store.load()

    expect(store.shown().map((row) => row.id)).toEqual(['alpha', 'beta'])
  })

  it('orders the recent tab by when a base was made, newest first', async () => {
    install([
      base({ id: 'old', created_at: '2026-01-01T00:00:00Z' }),
      base({ id: 'new', created_at: '2026-06-01T00:00:00Z' }),
      base({ id: 'mid', created_at: '2026-03-01T00:00:00Z' }),
    ])
    await store.load()
    store.setTab('recent')

    expect(store.shown().map((row) => row.id)).toEqual(['new', 'mid', 'old'])
  })

  it('shows only what the reader starred on the starred tab', async () => {
    install([base({ id: 'a', starred: true }), base({ id: 'b' }), base({ id: 'c', starred: true })])
    await store.load()
    store.setTab('starred')

    expect(store.shown().map((row) => row.id)).toEqual(['a', 'c'])
  })

  it('leads every tab with what is pinned', async () => {
    /* The pin is about order, so a reader who set it meant it wherever they
       are looking -- including the tab that orders by date. */
    install([
      base({ id: 'newest', created_at: '2026-09-01T00:00:00Z' }),
      base({ id: 'pinned', created_at: '2026-01-01T00:00:00Z', pinned: true }),
    ])
    await store.load()

    expect(store.shown().map((row) => row.id)).toEqual(['pinned', 'newest'])
    store.setTab('recent')
    expect(store.shown().map((row) => row.id)).toEqual(['pinned', 'newest'])
  })

  it('puts the row a write answered with back in place', async () => {
    const fake = install([base({ id: 'a' })])
    await store.load()

    await store.write(store.get().bases![0]!, { starred: true })

    expect(fake.wrote).toEqual([['a', { starred: true }]])
    expect(store.get().bases![0]!.starred).toBe(true)
  })

  it('turns a mark off again', async () => {
    const fake = install([base({ id: 'a', pinned: true })])
    await store.load()

    store.togglePin(store.get().bases![0]!)
    await vi.waitFor(() => expect(fake.wrote.length).toBe(1))

    expect(fake.wrote[0]![1]).toEqual({ pinned: false })
  })

  it('deletes the base and takes its row with it', async () => {
    /* What asks first is the menu that raised this, which rewords its own row
       and takes a second press. The guard lives there rather than here, and
       it used to live in both: the menu armed, this armed again, and the
       reader's second press only re-armed. */
    const fake = install([base({ id: 'a' }), base({ id: 'b' })])
    await store.load()

    store.remove(store.get().bases![0]!)
    await vi.waitFor(() => expect(fake.removed).toEqual(['a']))

    expect(store.get().bases!.map((row) => row.id)).toEqual(['b'])
  })

  it('inserts what create answered rather than reading the list again', async () => {
    const fake = install([])
    await store.load()
    const reads = vi.spyOn(sources.knowledge!, 'bases')

    await store.create('handbook', 'ops')

    expect(store.get().bases!.map((row) => row.id)).toEqual(['handbook'])
    expect(reads).not.toHaveBeenCalled()
    expect(fake.removed).toEqual([])
  })

  it('refuses a base with no name', async () => {
    install([])
    await store.load()

    await store.create('   ', '')

    expect(store.get().bases).toEqual([])
  })
  it('shows only the documents the open folder holds', async () => {
    install([base({ id: 'a' })])
    await store.load()
    folders = [{ id: 'f1', name: 'specs', documents: 1 }]
    docs = [doc({ id: 'd1' }), doc({ id: 'd2', folder_id: 'f1' })]

    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().documents).toHaveLength(2))

    /* Root is where a document with no folder is, not a folder of its own. */
    expect(store.shownDocs().map((row) => row.id)).toEqual(['d1'])
    store.setFolder('f1')
    expect(store.shownDocs().map((row) => row.id)).toEqual(['d2'])
  })

  it('files an upload under the folder the reader is standing in', async () => {
    /* Having opened a folder is the answer to where this goes; landing it in
       Root would make the reader move every file they just added. */
    install([base({ id: 'a' })])
    await store.load()
    folders = [{ id: 'f1', name: 'specs', documents: 0 }]
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().folders).toHaveLength(1))
    store.setFolder('f1')

    await store.upload('handbook.pdf', 'Ynl0ZXM=')

    expect(added).toEqual(['handbook.pdf'])
    expect(store.shownDocs().map((row) => row.source)).toEqual(['handbook.pdf'])
  })

  it('indexes what it just added rather than leaving it queued', async () => {
    /* Adding and indexing are two calls on the contract and nothing drains
       the queue on its own, so an add that stops after the first one leaves a
       row that says Queued for good. */
    install([base({ id: 'a' })])
    await store.load()
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().documents).toEqual([]))

    await store.upload('handbook.pdf', 'Ynl0ZXM=')

    expect(indexed).toEqual(['d-handbook.pdf'])
    expect(store.get().documents!.map((row) => row.status)).toEqual(['ready'])
  })

  it('reads the list again when indexing did not come back', async () => {
    /* The status is the server's to say; a throw leaves this list's copy of
       it a guess. */
    install([base({ id: 'a' })])
    await store.load()
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().documents).toEqual([]))
    indexFails = 'no embedding endpoint'
    const reads = vi.spyOn(sources.knowledge!, 'documents')

    await store.upload('handbook.pdf', 'Ynl0ZXM=')

    expect(reads).toHaveBeenCalledTimes(1)
    expect(store.get().documents!.map((row) => row.status)).toEqual(['pending'])
  })

  it('leaves an upload in Root when that is where the reader is', async () => {
    install([base({ id: 'a' })])
    await store.load()
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().documents).toEqual([]))

    await store.addUrl('  https://example.test/paper  ')

    expect(added).toEqual(['https://example.test/paper'])
    expect(store.get().documents!.map((row) => row.folder_id)).toEqual([''])
  })

  it('adds nothing for a url or a note with no content', async () => {
    install([base({ id: 'a' })])
    await store.load()
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().documents).toEqual([]))

    await store.addUrl('   ')
    await store.addNote('   ', 'body')

    expect(added).toEqual([])
  })

  it('names a folder and puts it in the list', async () => {
    install([base({ id: 'a' })])
    await store.load()
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().folders).toEqual([]))

    await store.createFolder('  specs  ')

    expect(store.get().folders.map((row) => row.name)).toEqual(['specs'])
  })

  it('refuses a folder with no name', async () => {
    install([base({ id: 'a' })])
    await store.load()
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().folders).toEqual([]))

    await store.createFolder('   ')

    expect(store.get().folders).toEqual([])
  })

  it('returns a deleted folder\'s documents to Root, and the reader with them', async () => {
    install([base({ id: 'a' })])
    await store.load()
    folders = [{ id: 'f1', name: 'specs', documents: 1 }]
    docs = [doc({ id: 'd1', folder_id: 'f1' })]
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().folders).toHaveLength(1))
    store.setFolder('f1')

    await store.removeFolder(store.get().folders[0]!)

    expect(store.get().folders).toEqual([])
    /* Standing in a folder that is gone would show an empty list and no way
       to tell that from a folder with nothing in it. */
    expect(store.get().folder).toBe('')
    expect(store.shownDocs().map((row) => row.id)).toEqual(['d1'])
  })

  it('files one document where the move said, and forgets the sheet', async () => {
    install([base({ id: 'a' })])
    await store.load()
    folders = [{ id: 'f1', name: 'specs', documents: 0 }]
    docs = [doc({ id: 'd1' })]
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().documents).toHaveLength(1))
    store.openMove(store.get().documents![0]!)

    await store.move(store.get().documents![0]!, 'f1')

    expect(store.get().moving).toBeNull()
    expect(store.get().documents!.map((row) => row.folder_id)).toEqual(['f1'])
  })

  it('goes back to Root and an empty folder list on the way into a base', async () => {
    /* A folder chosen in one base names nothing in the next, and a list
       filtered by it would read as a base with no documents at all. */
    install([base({ id: 'a' }), base({ id: 'b' })])
    await store.load()
    folders = [{ id: 'f1', name: 'specs', documents: 0 }]
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().folders).toHaveLength(1))
    store.setFolder('f1')

    folders = []
    store.openBase(store.get().bases![1]!)

    expect(store.get().folder).toBe('')
    await vi.waitFor(() => expect(store.get().folders).toEqual([]))
  })

  it('reads a document\'s pieces when it is opened, in reading order', async () => {
    /* Ordered here rather than trusted from the wire: a scan of the store has
       no order to promise, and reading order is what the panel claims. */
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 2 }), chunk({ chunk_index: 0 }), chunk({ chunk_index: 1 })]
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().documents).toHaveLength(1))

    store.openDoc(store.get().documents![0]!)

    expect(store.get().viewing?.id).toBe('d1')
    await vi.waitFor(() => expect(store.get().chunks).toHaveLength(3))
    expect(store.get().chunks!.map((row) => row.chunk_index)).toEqual([0, 1, 2])
  })

  it('waits rather than showing nothing while the pieces are read', async () => {
    /* Null is not the empty list: one is "wait", the other is "there is
       nothing here", and a panel that showed the same for both would be lying
       half the time. */
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().documents).toHaveLength(1))

    store.openDoc(store.get().documents![0]!)

    expect(store.get().chunks).toBeNull()
    await vi.waitFor(() => expect(store.get().chunks).toEqual([]))
  })

  it('says why the pieces could not be read', async () => {
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    chunksFail = 'the collection is gone'
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().documents).toHaveLength(1))

    store.openDoc(store.get().documents![0]!)

    await vi.waitFor(() => expect(store.get().chunksFailed).toBe('the collection is gone'))
    expect(store.get().chunks).toEqual([])
  })

  it('drops the pieces of a document the reader has already left', async () => {
    /* Clicking down a list faster than the engine answers would otherwise show
       one file's pieces under another file's name. */
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' }), doc({ id: 'd2' })]
    chunks = [chunk({ chunk_index: 0 })]
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().documents).toHaveLength(2))

    store.openDoc(store.get().documents![0]!)
    store.closeDoc()
    await vi.waitFor(() => expect(store.get().viewing).toBeNull())

    expect(store.get().chunks).toBeNull()
  })

  it('leaves the viewer when the document in it is deleted', async () => {
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().documents).toHaveLength(1))
    store.openDoc(store.get().documents![0]!)

    await store.removeDoc(store.get().viewing!)

    expect(store.get().viewing).toBeNull()
    expect(removedDocs).toEqual(['d1'])
  })

  it('renders markdown rather than framing it, and frames what a browser draws', async () => {
    /* The gateway serves .md as text/plain -- correctly -- and a frame then
       shows the hashes and the pipes, which is the file, not the document. */
    expect(store.previewKind(doc({ id: 'd1', source: 'notes.md' }))).toBe('markdown')
    expect(store.previewKind(doc({ id: 'd2', source: 'report.pdf' }))).toBe('native')
    expect(store.previewKind(doc({ id: 'd3', source: 'deck.pptx' }))).toBe('converted')
    expect(store.previewKind(doc({ id: 'd4', source: 'odd.thing' }))).toBe('none')
    /* An office file is converted on the way out; nothing else asks for it. */
    expect(store.previewUrl(doc({ id: 'd3', source: 'deck.pptx' }))).toContain('render=pdf')
    expect(store.previewUrl(doc({ id: 'd2', source: 'report.pdf' }))).not.toContain('render=pdf')
  })
  it('asks for the pieces a query answers, and keeps that ranking', async () => {
    /* A search answers best-first, and renumbering that into reading order
       would throw away the only thing the ranking had to say. */
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0, text: 'backups run nightly' }), chunk({ chunk_index: 1, text: 'on call rota' })]
    await openFirstDoc()

    store.setChunkQuery('backups')
    await vi.waitFor(() => expect(store.get().chunks).toHaveLength(1))

    expect(asked.at(-1)).toEqual({ page: 1, available: null, query: 'backups' })
    expect(store.get().chunks!.map((row) => row.text)).toEqual(['backups run nightly'])
    expect(store.get().chunkTotal).toBe(1)
  })

  it('asks for one state when the filter names one', async () => {
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0 }), chunk({ chunk_index: 1, enabled: false })]
    await openFirstDoc()

    store.setChunkFilter('off')
    await vi.waitFor(() => expect(store.get().chunks).toHaveLength(1))

    expect(asked.at(-1)!.available).toBe(false)
    expect(store.get().chunks!.map((row) => row.chunk_index)).toEqual([1])
  })

  it('goes back to the first page when the filter moves', async () => {
    /* A reader on page three of everything who asks for the disabled ones is
       not asking for page three of those. */
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    chunks = Array.from({ length: store.CHUNK_PAGE * 2 }, (_, at) => chunk({ chunk_index: at }))
    await openFirstDoc()

    store.setChunkPage(2)
    await vi.waitFor(() => expect(store.get().chunkPage).toBe(2))
    store.setChunkFilter('on')
    await vi.waitFor(() => expect(asked.at(-1)!.available).toBe(true))

    expect(store.get().chunkPage).toBe(1)
    expect(asked.at(-1)!.page).toBe(1)
  })

  it('will not page past either end', async () => {
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    chunks = Array.from({ length: store.CHUNK_PAGE + 1 }, (_, at) => chunk({ chunk_index: at }))
    await openFirstDoc()

    store.setChunkPage(0)
    expect(store.get().chunkPage).toBe(1)
    store.setChunkPage(9)
    await vi.waitFor(() => expect(store.get().chunkPage).toBe(2))
    expect(store.get().chunks).toHaveLength(1)
  })

  it('turns pieces off without re-reading the list', async () => {
    /* Nothing moved: under no filter the piece belongs where it already is,
       and a re-read would scroll a reader back to the top for nothing. */
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0 }), chunk({ chunk_index: 1 })]
    await openFirstDoc()
    const reads = asked.length

    await store.switchChunks(['c0'], false)

    expect(switched).toEqual([[['c0'], false]])
    expect(store.get().chunks!.map((row) => row.enabled)).toEqual([false, true])
    expect(asked).toHaveLength(reads)
  })

  it('re-reads the list when the switch moves a piece out of the filter', async () => {
    /* Under "enabled only", a piece just disabled is not in this list any
       more, and leaving it there would show a row the filter denies. */
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0 }), chunk({ chunk_index: 1 })]
    await openFirstDoc()
    store.setChunkFilter('on')
    await vi.waitFor(() => expect(store.get().chunks).toHaveLength(2))

    await store.switchChunks(['c0'], false)

    expect(store.get().chunks!.map((row) => row.chunk_index)).toEqual([1])
  })

  it('ticks what a batch acts on, and never a piece with no id', async () => {
    /* A piece written before chunk ids existed has nothing to address it: it
       can be read, not acted on. */
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0 }), chunk({ chunk_index: 1, chunk_id: '' })]
    await openFirstDoc()

    store.pickAll(true)
    expect(store.get().picked).toEqual(['c0'])

    store.pick(store.get().chunks![1]!)
    expect(store.get().picked).toEqual(['c0'])

    store.pick(store.get().chunks![0]!)
    expect(store.get().picked).toEqual([])
  })

  it('takes pieces out and reads what is left', async () => {
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0 }), chunk({ chunk_index: 1 }), chunk({ chunk_index: 2 })]
    await openFirstDoc()

    await store.deleteChunks(['c0', 'c1'])

    expect(killed).toEqual([['c0', 'c1']])
    expect(store.get().chunks!.map((row) => row.chunk_index)).toEqual([2])
    expect(store.get().chunkTotal).toBe(1)
  })

  it('appends a piece a person wrote, and shuts the draft', async () => {
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0 })]
    await openFirstDoc()
    store.draft(true)

    await store.createChunk('  a paragraph nobody parsed  ')

    expect(store.get().drafting).toBe(false)
    expect(store.get().chunks!.map((row) => row.text)).toEqual(['piece 0', 'a paragraph nobody parsed'])
    expect(store.get().chunks!.at(-1)!.manual).toBe(true)
  })

  it('writes nothing for a piece with no text', async () => {
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    await openFirstDoc()

    await store.createChunk('   ')

    expect(store.get().chunks).toEqual([])
  })

  it('rewrites one piece in place', async () => {
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0 }), chunk({ chunk_index: 1 })]
    await openFirstDoc()
    store.edit('c1')

    await store.updateChunk(store.get().chunks![1]!, 'said differently')

    expect(store.get().editing).toBe('')
    expect(store.get().chunks!.map((row) => row.text)).toEqual(['piece 0', 'said differently'])
  })

  it('forgets a query and a filter on the way into the next document', async () => {
    /* A query typed against one document says nothing about the next, and a
       list filtered by a search the reader cannot see reads as a document
       with three pieces in it. */
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' }), doc({ id: 'd2' })]
    chunks = [chunk({ chunk_index: 0 })]
    await openFirstDoc()
    store.setChunkQuery('backups')
    store.setChunkFilter('off')
    await vi.waitFor(() => expect(asked.at(-1)!.available).toBe(false))

    store.openDoc(store.get().documents![1]!)
    await vi.waitFor(() => expect(store.get().chunks).not.toBeNull())

    expect(store.get().chunkQuery).toBe('')
    expect(store.get().chunkFilter).toBe('all')
    expect(asked.at(-1)).toEqual({ page: 1, available: null, query: '' })
  })
  it('reads the file\'s pages when it is opened, and forgets them on the way out', async () => {
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    sheets = [{ number: 1, width: 595, height: 842 }]
    await openFirstDoc()

    await vi.waitFor(() => expect(store.get().pages).toHaveLength(1))
    expect(store.get().pages![0]).toEqual({ number: 1, width: 595, height: 842 })

    store.closeDoc()
    /* Null, not the empty list: the next document has not been read yet, and
       empty is what a format with no pages answers. */
    expect(store.get().pages).toBeNull()
  })

  it('says a format has no pages rather than failing the panel', async () => {
    /* There is nothing here a reader can act on -- the file is still shown,
       framed instead of drawn -- so a message would report the absence of a
       picture as a fault. */
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    sheets = []
    await openFirstDoc()

    await vi.waitFor(() => expect(store.get().pages).toEqual([]))
    expect(store.get().chunksFailed).toBeNull()
  })

  it('addresses one page of one document and nothing else', async () => {
    expect(store.pageUrl('d 1', 4)).toBe('/knowledge/page?document=d%201&page=4')
  })
  it('says a rebuild has started before it has finished', async () => {
    /* Parsing and embedding a long file runs for tens of seconds, and a row
       that does not move until it is over reads as a press that did nothing. */
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1', status: 'ready' })]
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().documents).toHaveLength(1))

    let mid = ''
    sources.knowledge!.reindex = async (id) => {
      mid = store.get().documents![0]!.status
      docs = docs.map((row) => (row.id === id ? { ...row, status: 'ready', chunk_count: 9 } : row))
      return docs[0]!
    }
    await store.reindex(store.get().documents![0]!)

    expect(mid).toBe('indexing')
    expect(store.get().documents![0]!.chunk_count).toBe(9)
  })

  it('reads the pieces again when the document being read is rebuilt', async () => {
    /* A rebuild deletes every piece and cuts the file again, so the ones on
       screen are no longer in the index and the one it was scrolled to
       addresses nothing. */
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    chunks = [chunk({ chunk_index: 0, regions: [{ page_number: 1, x0: 0, top: 10, x1: 9, bottom: 20 }] })]
    await openFirstDoc()
    store.aimAt(store.get().chunks![0]!)
    expect(store.get().aimed).not.toBe('')

    chunks = [chunk({ chunk_index: 0, text: 'cut again' }), chunk({ chunk_index: 1, text: 'and again' })]
    await store.reindex(store.get().viewing!)

    await vi.waitFor(() => expect(store.get().chunks).toHaveLength(2))
    expect(store.get().chunks!.map((row) => row.text)).toEqual(['cut again', 'and again'])
    /* And nothing is aimed at: the piece it was pointing to is gone. */
    expect(store.get().aimed).toBe('')
    expect(store.get().aim).toBeNull()
  })

  it('reads the base again after a rebuild, because what it holds has moved', async () => {
    /* The piece count is what decides whether the embedding model can still
       be changed. */
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1' })]
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().documents).toHaveLength(1))
    const reads = vi.spyOn(sources.knowledge!, 'bases')

    await store.reindex(store.get().documents![0]!)

    expect(reads).toHaveBeenCalled()
  })

  it('puts the row back as the server has it when a rebuild throws', async () => {
    install([base({ id: 'a' })])
    await store.load()
    docs = [doc({ id: 'd1', status: 'ready' })]
    store.openBase(store.get().bases![0]!)
    await vi.waitFor(() => expect(store.get().documents).toHaveLength(1))
    sources.knowledge!.reindex = async () => {
      throw new Error('no embedding endpoint')
    }

    await store.reindex(store.get().documents![0]!)

    /* Not left saying indexing: this list marked it, no answer did. */
    expect(store.get().documents![0]!.status).toBe('ready')
  })
})
