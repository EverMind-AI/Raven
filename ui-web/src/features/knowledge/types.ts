/* What the knowledge page draws, and the seam it reads it through.
 *
 * One base as the page needs it, which is fewer fields than the contract
 * carries: the card shows a name, a line about it, how many documents are in
 * it, and the reader's two marks. The tuning settings ride along because the
 * settings sheet edits them without a second fetch.
 */

/** One knowledge base, as a card draws it. */
export interface KbBase {
  id: string
  name: string
  description: string
  /** How many documents are in it. */
  documents: number
  /** ISO 8601, and the whole of what the recent tab orders by. */
  created_at: string
  /** Kept at the head of the list. */
  pinned: boolean
  /** Marked by the reader, and the whole of what the starred tab shows. */
  starred: boolean
  /** Empty for a base with no embedding model, which cannot be searched. */
  embedding_model: string
  /** Which account that model is reached through. A model id names no
   *  credential, so the pair travels together. */
  embedding_provider: string
  /** How many pieces the base holds. Not how many documents are in it -- one
   *  that failed, one still queued and one in a base with no model all count
   *  as documents and hold nothing. Zero is what makes the model safe to
   *  move: there is no index to throw away. */
  chunks: number
  /** How the base is tuned: how many pieces a search brings back, and how the
   *  chunker cuts what goes into it. Carried on the row rather than fetched
   *  when the settings open, because the list already answered with them. */
  top_k: number
  smart_chunking: boolean
  separator: string
  chunk_size: number
  chunk_overlap: number
  table_context_size: number
  image_context_size: number
}

/** One document in a base, as a row draws it. */
export interface KbDoc {
  id: string
  /** What it was uploaded as, which is the name a reader knows it by. */
  source: string
  /** `pending`, `indexing`, `ready` or `failed`. */
  status: string
  /** How many pieces it was cut into. Zero until it has been indexed. */
  chunk_count: number
  /** Bytes, as the upload measured. */
  size: number
  /** What kind of file it is, for the glyph beside its name. */
  media_type: string
  /** ISO 8601. What the list's last column shows. */
  updated_at: string
  /** Which folder it is filed under. Empty is Root. */
  folder_id: string
  /** Why it failed, and what the parse could not do even though it did not
   *  fail. Both are the server's own sentence, shown as-is. */
  error: string
  warning: string
}

/** One indexed piece of a document, as the panel beside it draws it.
 *
 *  Fewer fields than the contract carries: what a reader scanning this list
 *  uses is where the piece sits and what the parser knew about it. An absent
 *  field means the format does not know -- a text file has no pages -- never
 *  that the value is zero. */
export interface KbChunk {
  /** Where the piece sits in its document, and the reading order itself: the
   *  chunker numbers pieces as it walks the sections the parser produced. */
  chunk_index: number
  total_chunks: number
  text: string
  /** What the region is, as the source marked it. Empty where it did not. */
  layout_type: string
  /** The 1-based page it starts on, and the one it ends on where it ran over
   *  a boundary. Null for a format with no pages. */
  page_number: number | null
  page_end: number | null
  /** The headings it sits under, outermost first. */
  heading_path: string[]
  /** What addresses it. Empty on rows written before ids existed. */
  chunk_id: string
  /** Whether it may be retrieved at all. */
  enabled: boolean
  /** Whether a person wrote it rather than the chunker cutting it. */
  manual: boolean
  /** Whether a picture of the region it was cut from is stored for it. */
  has_crop: boolean
  /** Every place on a page it was cut from, in reading order. Empty for a
   *  format with no pages and for a piece a person wrote. A piece that merged
   *  several can cross a page boundary, so this can name more than one. */
  regions: KbRegion[]
}

/** One page of a document, as the panel draws it. Its size is in the same
 *  points a region is measured in, which is what makes a region placeable:
 *  a fraction of the width across, a fraction of the height down. */
export interface KbPage {
  number: number
  width: number
  height: number
}

/** One rectangle on one page, in PDF points from the page's top-left. */
export interface KbRegion {
  page_number: number
  x0: number
  top: number
  x1: number
  bottom: number
}

/** What one read of the chunk list asks for. */
export interface KbChunkAsk {
  /** 1-based page of the reading order. */
  page: number
  /** Only the pieces in this state; null for both. */
  available: boolean | null
  /** When set, the pieces that answer it rather than a page of the order. */
  query: string
}

/** One page of them, and how many the filter admits in total. */
export interface KbChunkPage {
  chunks: KbChunk[]
  total: number
}

/** One embedding model a base can be built on, and who serves it. */
export interface KbModel {
  id: string
  /** How it reads to a person. The id where the registry knows nothing. */
  label: string
  /** The account it is reached through. A model id names no credential, so
   *  the pair travels together. */
  provider: string
  providerName: string
}

/** One folder inside a base. Root is not one of these -- it is the absence of
 *  one, so a document with no folder is in Root. */
export interface KbFolder {
  id: string
  name: string
  /** How many documents are filed under it. */
  documents: number
}

/** What one settings write may change. Every field is optional: the sheet
 *  saves what the reader touched, and a field left out is left alone.
 *
 *  The embedding model is not among them. Sending it rebuilds the collection,
 *  which is not a thing a settings panel should be able to do by being saved. */
export interface KbSettings {
  name?: string
  description?: string
  pinned?: boolean
  starred?: boolean
  /** How many pieces a search over this base brings back. */
  top_k?: number
  /** Whether the chunker cuts on the document's own structure. With it off,
   *  `separator` is what it cuts on instead. */
  smart_chunking?: boolean
  separator?: string
  /** Tokens. What the chunker aims for, and how much of each piece the next
   *  one repeats. */
  chunk_size?: number
  chunk_overlap?: number
  /** Tokens of the surrounding prose carried into a chunk that is only a
   *  table or only a figure. Zero carries none. */
  table_context_size?: number
  image_context_size?: number
  /** Moving the base onto another model, which is not a setting: the
   *  collection is made again at the new width and every document goes back
   *  to the queue. Empty turns embedding off. The pair travels together. */
  embedding_model?: string
  embedding_provider?: string
}

/** The seam the island reads. One responder, so a page with no gateway behind
 *  it draws the same cards off the fixture transport. */
export interface KnowledgeSource {
  bases(): Promise<KbBase[]>
  /** Every embedding model the configured providers offer. */
  models(): Promise<KbModel[]>
  documents(baseId: string): Promise<KbDoc[]>
  /** One document's pages, numbered and measured. Empty for a format that
   *  has none, which is not a failure -- the panel frames the file instead. */
  pages(documentId: string): Promise<KbPage[]>
  /** One page of a document's indexed pieces, and how many the filter
   *  admits. In the order the chunker made them, unless a query is given --
   *  then they are what answered it, best first. */
  chunks(documentId: string, ask: KbChunkAsk): Promise<KbChunkPage>
  /** Turn pieces on or off. Answers how many the store actually changed. */
  switchChunks(documentId: string, chunkIds: string[], enabled: boolean): Promise<number>
  /** Remove pieces. Answers how many the document has left. */
  deleteChunks(documentId: string, chunkIds: string[]): Promise<number>
  /** Append a piece a person wrote, embedded like every other piece. */
  createChunk(documentId: string, text: string): Promise<KbChunk>
  /** Rewrite one piece's text, which re-embeds it. */
  updateChunk(documentId: string, chunkId: string, text: string): Promise<KbChunk>
  folders(baseId: string): Promise<KbFolder[]>
  createFolder(baseId: string, name: string): Promise<KbFolder>
  removeFolder(id: string): Promise<void>
  /** File it under a folder, or under Root for an empty id. */
  move(id: string, folderId: string): Promise<KbDoc>
  /** Upload one file's bytes and add it to the base. */
  upload(baseId: string, name: string, contentB64: string): Promise<KbDoc>
  addUrl(baseId: string, url: string): Promise<KbDoc>
  addNote(baseId: string, title: string, text: string): Promise<KbDoc>
  /** Cut and embed it again, answering with the row as it now stands. */
  reindex(id: string): Promise<KbDoc>
  removeDoc(id: string): Promise<void>
  create(name: string, description: string): Promise<KbBase>
  settings(id: string, settings: KbSettings): Promise<KbBase>
  remove(id: string): Promise<void>
}
