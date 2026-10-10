/* -- knowledge bases: the rpc source ---------------------------------
   `knowledge.*` is one surface shared with the TUI: the bases, their settings
   and every write live server-side, so this module only maps a row into what
   the cards draw and sends the mutation back.

   A write answers with the row as it now stands, and the store puts that row
   in place rather than refetching the list. That is the server's own answer,
   not a guess: `knowledge.bases.settings` recomputes the record and returns
   it, so a card never shows a value the registry does not hold. The list is
   refetched only where a write changes which rows exist at all.
*/

import { gateway } from '../../rpc/gateway'

import type { ResultOf } from '../../rpc/generated'
import type {
  KbBase,
  KbChunk,
  KbChunkAsk,
  KbChunkPage,
  KbDoc,
  KbFolder,
  KbModel,
  KbPage,
  KbSettings,
  KnowledgeSource,
} from './types'

/* What a base is tuned to when nobody has tuned it. The same numbers the
   engine defaults to, held here so the panel can offer to put them back --
   a reader who has moved four fields needs one way out, not four. */
export const DEFAULTS = {
  top_k: 6,
  smart_chunking: true,
  separator: '\n\n',
  chunk_size: 2048,
  chunk_overlap: 215,
  table_context_size: 64,
  image_context_size: 64,
} as const

/** One base as `knowledge.bases.list` sends it. */
export type KbBaseWire = ResultOf<'knowledge.bases.list'>['bases'][number]

/* A whitelist, deliberately: a field this does not name is a field the island
   never sees, so the contract can grow without the page quietly rendering
   something nobody designed. */
export function baseOf(row: KbBaseWire): KbBase {
  return {
    id: row.id,
    name: row.name,
    description: row.description ?? '',
    documents: row.documents ?? 0,
    created_at: row.created_at ?? '',
    pinned: !!row.pinned,
    starred: !!row.starred,
    embedding_model: row.embedding_model ?? '',
    embedding_provider: row.embedding_provider ?? '',
    chunks: row.chunks ?? 0,
    top_k: row.top_k ?? DEFAULTS.top_k,
    smart_chunking: row.smart_chunking ?? DEFAULTS.smart_chunking,
    separator: row.separator ?? DEFAULTS.separator,
    chunk_size: row.chunk_size ?? DEFAULTS.chunk_size,
    chunk_overlap: row.chunk_overlap ?? DEFAULTS.chunk_overlap,
    table_context_size: row.table_context_size ?? DEFAULTS.table_context_size,
    image_context_size: row.image_context_size ?? DEFAULTS.image_context_size,
  }
}

/** One document as `knowledge.documents.list` sends it. */
export type KbDocWire = ResultOf<'knowledge.documents.list'>['documents'][number]

export function docOf(row: KbDocWire): KbDoc {
  return {
    id: row.id,
    source: row.source,
    status: row.status ?? '',
    chunk_count: row.chunk_count ?? 0,
    size: row.size ?? 0,
    media_type: row.media_type ?? '',
    updated_at: row.updated_at ?? '',
    folder_id: row.folder_id ?? '',
    error: row.error ?? '',
    warning: row.warning ?? '',
  }
}

/* How many pieces one page of the list holds. The method's own ceiling is
   100 and its default 20; a panel that is scrolled rather than clicked reads
   better with more of the document in it at once. */
export const CHUNK_PAGE = 50

/** One piece as `knowledge.documents.chunks` sends it. */
export type KbChunkWire = ResultOf<'knowledge.documents.chunks'>['chunks'][number]

export function chunkOf(row: KbChunkWire): KbChunk {
  return {
    chunk_index: row.chunk_index,
    total_chunks: row.total_chunks ?? 0,
    text: row.text,
    layout_type: row.layout_type ?? '',
    page_number: row.page_number ?? null,
    /* Only where it says something the first page does not: a chunk that
       stayed on one page reports the same number twice, and a panel drawing
       "3-3" is making a reader work out that it means 3. */
    page_end: row.page_end && row.page_end !== row.page_number ? row.page_end : null,
    heading_path: row.heading_path ?? [],
    chunk_id: row.chunk_id ?? '',
    enabled: row.enabled ?? true,
    manual: !!row.manual,
    has_crop: !!row.has_crop,
    regions: (row.regions ?? []).map((box) => ({
      page_number: box.page_number,
      x0: box.x0,
      top: box.top,
      x1: box.x1,
      bottom: box.bottom,
    })),
  }
}

export const knowledgeSource: KnowledgeSource = {
  async bases(): Promise<KbBase[]> {
    const out = await gateway().call('knowledge.bases.list', {})
    return (out.bases ?? []).map(baseOf)
  },

  async documents(baseId: string): Promise<KbDoc[]> {
    const out = await gateway().call('knowledge.documents.list', { base_id: baseId })
    return (out.documents ?? []).map(docOf)
  },

  /* Read back from the store rather than re-cut from the file: what a reader
     wants to see is what the search matches against, and a fresh parse shows
     what a rebuild would produce instead -- a different thing the moment a
     chunking setting has moved. */
  /* Every embedding model the install can reach, from the same read the
     settings page's Embedding row uses. Filtered by what the registry says a
     model is: an embedding slot listing a chat model is a pick that fails on
     the next call.

     Only providers with a credential: the picker offers what can be reached,
     and a name the account cannot call is a failure with a label on it. */
  async models(): Promise<KbModel[]> {
    const out = await gateway().call('model.options', {})
    const found: KbModel[] = []
    for (const provider of out.providers ?? []) {
      if (!provider.authenticated) continue
      for (const id of provider.models ?? []) {
        const label = provider.model_labels?.[id]
        if (label?.kind !== 'embedding') continue
        found.push({ id, label: label.label || id, provider: provider.slug, providerName: provider.name })
      }
    }
    return found
  },

  async pages(documentId: string): Promise<KbPage[]> {
    const out = await gateway().call('knowledge.documents.pages', { document_id: documentId })
    return (out.pages ?? []).map((row) => ({ number: row.number, width: row.width, height: row.height }))
  },

  async chunks(documentId: string, ask: KbChunkAsk): Promise<KbChunkPage> {
    const out = await gateway().call('knowledge.documents.chunks', {
      document_id: documentId,
      page: ask.page,
      page_size: CHUNK_PAGE,
      /* Omitted rather than sent as null for `all`: the method reads the key's
         absence as "both states", and a null it has to interpret is a second
         way of saying the same thing. */
      ...(ask.available === null ? {} : { available: ask.available }),
      ...(ask.query ? { query: ask.query } : {}),
    })
    return { chunks: (out.chunks ?? []).map(chunkOf), total: out.total ?? 0 }
  },

  async switchChunks(documentId: string, chunkIds: string[], enabled: boolean): Promise<number> {
    const out = await gateway().call('knowledge.chunks.switch', {
      document_id: documentId,
      chunk_ids: chunkIds,
      enabled,
    })
    return out.changed ?? 0
  },

  async deleteChunks(documentId: string, chunkIds: string[]): Promise<number> {
    const out = await gateway().call('knowledge.chunks.delete', { document_id: documentId, chunk_ids: chunkIds })
    return out.remaining ?? 0
  },

  async createChunk(documentId: string, text: string): Promise<KbChunk> {
    const out = await gateway().call('knowledge.chunks.create', { document_id: documentId, text })
    return chunkOf(out.chunk)
  },

  async updateChunk(documentId: string, chunkId: string, text: string): Promise<KbChunk> {
    const out = await gateway().call('knowledge.chunks.update', {
      document_id: documentId,
      chunk_id: chunkId,
      text,
    })
    return chunkOf(out.chunk)
  },

  async folders(baseId: string): Promise<KbFolder[]> {
    const out = await gateway().call('knowledge.folders.list', { base_id: baseId })
    return (out.folders ?? []).map((row) => ({ id: row.id, name: row.name, documents: row.documents ?? 0 }))
  },

  async createFolder(baseId: string, name: string): Promise<KbFolder> {
    const out = await gateway().call('knowledge.folders.create', { base_id: baseId, name })
    return { id: out.folder.id, name: out.folder.name, documents: out.folder.documents ?? 0 }
  },

  async removeFolder(id: string): Promise<void> {
    await gateway().call('knowledge.folders.delete', { folder_id: id })
  },

  async move(id: string, folderId: string): Promise<KbDoc> {
    const out = await gateway().call('knowledge.documents.move', { document_id: id, folder_id: folderId })
    return docOf(out.document)
  },

  /* Two calls, because bytes never ride inside an RPC message: `fs.upload`
     puts the file on the host and answers with a path, and the knowledge
     method takes it from there. The same pair the composer's attachments
     use. */
  async upload(baseId: string, name: string, contentB64: string): Promise<KbDoc> {
    const put = await gateway().call('fs.upload', { name, content_b64: contentB64, session: '' })
    const out = await gateway().call('knowledge.documents.add', { base_id: baseId, path: put.abs_path })
    return docOf(out.document)
  },

  async addUrl(baseId: string, url: string): Promise<KbDoc> {
    const out = await gateway().call('knowledge.documents.add_url', { base_id: baseId, url })
    return docOf(out.document)
  },

  async addNote(baseId: string, title: string, text: string): Promise<KbDoc> {
    const out = await gateway().call('knowledge.documents.add_note', { base_id: baseId, title, text })
    return docOf(out.document)
  },

  async reindex(id: string): Promise<KbDoc> {
    const out = await gateway().call('knowledge.documents.index', { document_id: id })
    return docOf(out.document)
  },

  async removeDoc(id: string): Promise<void> {
    await gateway().call('knowledge.documents.delete', { document_id: id })
  },

  async create(name: string, description: string): Promise<KbBase> {
    const out = await gateway().call('knowledge.bases.create', { name, description })
    return baseOf(out.base)
  },

  async settings(id: string, settings: KbSettings): Promise<KbBase> {
    /* Two calls where the reader changed the name, because naming a base and
       tuning one are different methods on the contract. The rename goes first
       so that a settings write failing does not leave the card showing a name
       the registry never took. */
    let row = null as KbBaseWire | null
    if (settings.name !== undefined || settings.description !== undefined) {
      const renamed = await gateway().call('knowledge.bases.rename', {
        base_id: id,
        name: settings.name ?? '',
        description: settings.description ?? '',
      })
      row = renamed.base
    }
    /* Everything that is not the name goes in one write: the marks a card
       carries and the tuning the panel edits are the same method, and a field
       left out is a field left alone. */
    const rest: Record<string, unknown> = {}
    if (settings.pinned !== undefined) rest.pinned = settings.pinned
    if (settings.starred !== undefined) rest.starred = settings.starred
    if (settings.top_k !== undefined) rest.top_k = settings.top_k
    if (settings.smart_chunking !== undefined) rest.smart_chunking = settings.smart_chunking
    if (settings.separator !== undefined) rest.separator = settings.separator
    if (settings.chunk_size !== undefined) rest.chunk_size = settings.chunk_size
    if (settings.chunk_overlap !== undefined) rest.chunk_overlap = settings.chunk_overlap
    if (settings.table_context_size !== undefined) rest.table_context_size = settings.table_context_size
    if (settings.image_context_size !== undefined) rest.image_context_size = settings.image_context_size
    /* The pair or neither: sending a model without the account it is reached
       through would have the engine resolve it against the configured one. */
    if (settings.embedding_model !== undefined) {
      rest.embedding_model = settings.embedding_model
      rest.embedding_provider = settings.embedding_provider ?? ''
    }
    if (Object.keys(rest).length) {
      const written = await gateway().call('knowledge.bases.settings', { base_id: id, ...rest })
      row = written.base
    }
    if (!row) throw new Error('nothing to write')
    return baseOf(row)
  },

  async remove(id: string): Promise<void> {
    await gateway().call('knowledge.bases.delete', { base_id: id })
  },
}
