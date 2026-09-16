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
  file_processing?: string
}

/* What the settings panel can write. Every field optional: the ones left out
   are untouched, so a panel need not send back what it did not change. */
export interface KbSettings {
  top_k?: number
  smart_chunking?: boolean
  separator?: string
  chunk_size?: number
  chunk_overlap?: number
  file_processing?: string
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
     searched by vector. Not revisable: a collection's width is fixed when it
     is made, so the choice belongs to creation or nowhere. */
  create(name: string, description: string, embedding?: boolean): Promise<KbBase>
  /* A base's name, which is the one thing about it that carries no index
     consequence. Refused when another base already holds it. */
  rename(id: string, name: string): Promise<KbBase>
  remove(id: string): Promise<unknown>
  /* Write one base's settings. Not the embedding model: the store is sized to
     its vector width, so changing it is a rebuild rather than a setting. */
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
  /* Take one document out. The page's only way past a row that will not
     index: without it the base around it is the smallest thing that can be
     deleted. */
  removeDoc(documentId: string): Promise<void>
  search(baseIds: string[], query: string, topK?: number): Promise<KbSearch>
}

/* One hit. `score` is a similarity, so higher is nearer. */
export interface KbHit {
  score: number
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
export interface KbSearch {
  hits: KbHit[]
  search_ms: number
  embed_ms: number
}
