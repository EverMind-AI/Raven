/* One base as `knowledge.bases.list` answers it.

   `embedding_model` and `dimensions` are the base's own, recorded when it was
   created rather than read from today's config: a base outlives a change to
   what the operator has configured, and the page has to be able to show that
   rather than search against it silently. */
export interface KbBase {
  id: string
  name: string
  description: string
  embedding_model: string
  dimensions: number
  created_at: string
  updated_at: string
  documents: number
  /* The settings panel's fields. Optional so a gateway that predates them
     still answers a shape this page can read. */
  top_k?: number
  smart_chunking?: boolean
  separator?: string
  chunk_size?: number
  chunk_overlap?: number
  /* Tokens of the prose around a table or a figure to carry into the chunk
     that holds it. Read by the naive strategy, where a table is its own
     chunk. */
  table_context_size?: number
  image_context_size?: number
  file_processing?: string
  /* Where this base's model is reached, when it is not the configured one. */
  embedding_provider?: string
  /* Empty when the model can be reached; otherwise why not -- `no_provider`
     or `no_credential`. Answered from what is recorded, so an endpoint that is
     merely down still reads as reachable. */
  embedding_reach?: string
}

/* One provider and the embedding models it serves, as the picker offers them.
   Grouped rather than flat because the group is the answer to "whose
   credential pays for this", which a bare model id does not carry. */
export interface KbProvider {
  id: string
  name: string
  models: string[]
}

/* What the settings panel can write. Every field optional: the ones left out
   are untouched, so a panel need not send back what it did not change. */
export interface KbSettings {
  top_k?: number
  smart_chunking?: boolean
  separator?: string
  chunk_size?: number
  chunk_overlap?: number
  table_context_size?: number
  image_context_size?: number
  file_processing?: string
  /* Where the base's model is reached. Sent alone it moves only the address. */
  embedding_provider?: string
  /* The model itself. Not a setting: sending it rebuilds the base, because
     the collection is sized to the model's width and holds vectors that model
     made. Empty turns embedding off. */
  embedding_model?: string
}

/* One document and where its indexing got to. `error` is empty unless `status`
   is `failed`; the row carries the reason with it, so a reader does not have to
   go looking for why nothing is searchable. */
export interface KbDoc {
  id: string
  base_id: string
  source: string
  media_type: string
  size: number
  status: string
  chunk_count: number
  error: string
  /* What the parse could not do, on a document that was indexed anyway. Not a
     second `error`: this row is `ready` and searchable, and the line says
     which part of the file is not in the index -- pictures no model could
     read, most often. Optional so a gateway that predates it still answers a
     shape this page can read. */
  warning?: string
  created_at: string
  updated_at: string
  /* Which kind of data source this arrived through. A folder is not one of
     them: the browser walks it and sends the files, so each lands as a file. */
  origin?: 'file' | 'note' | 'url'
  /* Where a url document was read from. Empty for every other origin. */
  origin_ref?: string
}

/* Whether a base could be created at all, and with which model. `model` is
   empty exactly when `configured` is false. No credential crosses the wire --
   a key being present is the flag. */
export interface KbStatus {
  configured: boolean
  model: string
  /* Who serves that model. The pair is what the picker preselects with: a
     model id on its own names no credential and matches no option. */
  provider?: string
  /* Extensions this build can index, each with its leading dot. What a folder
     walk filters by -- reported rather than listed in the page, because which
     formats are parseable moves with the optional extras installed. */
  extensions?: string[]
}

/* The DS.knowledge contract both the fixture source (demo shell) and the rpc
   source (live layer) implement. The island only ever talks to this, which is
   what lets the page be developed and tested with no engine behind it. */
export interface KnowledgeSource {
  status(): Promise<KbStatus>
  bases(): Promise<KbBase[]>
  /* ``embedding`` false makes a base that keeps its documents and is never
     searched by vector. ``model`` and ``provider`` are the pair the picker
     chose; left out, the base is built on the configured default. */
  create(
    name: string,
    description: string,
    embedding?: boolean,
    model?: string,
    provider?: string,
  ): Promise<KbBase>
  /* A base's name, which is the one thing about it that carries no index
     consequence. Refused when another base already holds it. */
  rename(id: string, name: string): Promise<KbBase>
  remove(id: string): Promise<unknown>
  /* Write one base's settings. Sending `embedding_model` is the exception: it
     rebuilds the base rather than writing a value, and every document in it
     goes back to the queue to be indexed again. */
  settings(baseId: string, values: KbSettings): Promise<KbBase>
  documents(baseId: string): Promise<KbDoc[]>
  /* Two calls behind one name: the bytes go up through `fs.upload`, which is
     the one path the gateway will read from, and the base is then told about
     the file it left there. The island has no reason to know that. */
  upload(baseId: string, file: File): Promise<KbDoc>
  /* A typed note, kept as the markdown it was written in. `title` may be
     empty; the note is then named from its first line. */
  addNote(baseId: string, title: string, text: string): Promise<KbDoc>
  /* Rewrite one note. Only a note: every other origin is a copy of something
     the reader holds elsewhere, and editing it here would make this base the
     only place the change exists. */
  updateNote(documentId: string, title: string, text: string): Promise<KbDoc>
  /* One web page, read by the gateway -- a browser cannot fetch a third-party
     site on the reader's behalf -- and kept as markdown. */
  addUrl(baseId: string, url: string): Promise<KbDoc>
  index(documentId: string): Promise<KbDoc>
  /* One document's indexed pieces, in reading order. What the search matches
     against, not a fresh parse: the two stop agreeing as soon as a chunking
     setting has moved. */
  chunks(documentId: string, opts?: KbChunkQuery): Promise<KbChunkPage>
  /* Turn pieces on or off. Off is out of retrieval entirely. */
  switchChunks(documentId: string, chunkIds: string[], enabled: boolean): Promise<number>
  /* Remove pieces. Unlike disabling, nothing is kept. Answers with how many
     the document has left. */
  deleteChunks(documentId: string, chunkIds: string[]): Promise<number>
  /* Append a piece a person wrote, embedded like every other piece. */
  createChunk(documentId: string, text: string): Promise<KbChunk>
  /* Rewrite one piece, re-embedding it so the vector says what it says.
     Its id changes with its text, because ids are derived from content. */
  updateChunk(documentId: string, chunkId: string, text: string): Promise<KbChunk>
  /* Every embedding model this install can reach, grouped by the provider
     serving it. What both pickers offer: the one a base is created with and
     the one that moves an existing base onto another model. */
  embeddingModels(): Promise<KbProvider[]>
  /* Take one document out. The page's only way past a row that will not
     index: without it the base around it is the smallest thing that can be
     deleted. */
  removeDoc(documentId: string): Promise<void>
  search(baseIds: string[], query: string, topK?: number): Promise<KbSearch>
}

/* One hit. `score` runs one direction whatever found it -- higher is nearer --
   and `retrieval` says what the number is. */
/* One indexed piece of a document, as the search sees it.

   The positional fields are there for the formats that have them -- a page and
   a layout type come off a Word file or a PDF, and a text file has neither.
   Absent means the parser did not know, never that the value is zero. */
export interface KbChunk {
  /* Where the piece sits in its document, and how many there are. This is the
     reading order: the chunker numbers pieces as it walks the sections the
     parser produced, so the sequence is the document's own. */
  chunk_index: number
  total_chunks: number
  text: string
  layout_type?: string
  page_number?: number | null
  heading_path?: string[]
  /* What addresses this piece. Derived from its text, so it survives a rebuild
     of the same document. */
  chunk_id?: string
  /* Whether it may be retrieved. Off is not a ranking penalty: a disabled
     piece is never searched and never reaches the agent. */
  enabled?: boolean
  /* Whether a person wrote it rather than a parser cutting it. It goes with
     every other piece when the document is reindexed. */
  manual?: boolean
}

/* How a page of chunks is asked for. Absent members mean "the first page of
   the reading order, both states" -- and a `query` replaces the page entirely
   with what matched, best first. */
export interface KbChunkQuery {
  page?: number
  page_size?: number
  available?: boolean | null
  query?: string
}

export interface KbChunkPage {
  chunks: KbChunk[]
  total: number
}

export interface KbHit {
  score: number
  /* How this hit was found: `vector` is a cosine similarity in 0..1,
     `keyword` a BM25 score on the index's own unbounded scale. The two are not
     comparable by value, so a surface showing both has to say which is which
     rather than printing them under one heading. Absent from a gateway that
     predates the field, which only ever searched by vector. */
  retrieval?: 'vector' | 'keyword'
  document_id: string
  text: string
  /* Which piece of its document this was, and of how many. A chunk read on
     its own says nothing about where in the document it came from. */
  chunk_index?: number
  total_chunks?: number
  /* What the chunk was parsed from, so a hit can be read even when the row it
     came from is no longer in the list. */
  source?: string
}

/* One search, with what each half of it cost. `search_ms` is the index doing
   its job; `embed_ms` is the round trip to the configured embedding endpoint,
   which is the larger number and the one that describes the provider rather
   than the retrieval. */
/* One base that answered by words, and why its vectors were out of reach. */
export interface KbFallback {
  base_id: string
  reason: string
}

export interface KbSearch {
  hits: KbHit[]
  /* The bases that answered by keyword rather than by meaning. Empty on an
     ordinary search; a reader comparing two sets of results has to be told
     that retrieval changed mode, because the hits themselves look normal. */
  by_keyword?: KbFallback[]
  search_ms: number
  embed_ms: number
}
