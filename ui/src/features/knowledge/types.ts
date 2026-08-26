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
}

/* Whether a base could be created at all, and with which model. `model` is
   empty exactly when `configured` is false. No credential crosses the wire --
   a key being present is the flag. */
export interface KbStatus {
  configured: boolean
  model: string
}

/* The DS.knowledge contract both the fixture source (demo shell) and the rpc
   source (live layer) implement. The island only ever talks to this, which is
   what lets the page be developed and tested with no engine behind it. */
export interface KnowledgeSource {
  status(): Promise<KbStatus>
  bases(): Promise<KbBase[]>
  create(name: string, description: string): Promise<KbBase>
  remove(id: string): Promise<unknown>
  documents(baseId: string): Promise<KbDoc[]>
  /* Two calls behind one name: the bytes go up through `fs.upload`, which is
     the one path the gateway will read from, and the base is then told about
     the file it left there. The island has no reason to know that. */
  upload(baseId: string, file: File): Promise<KbDoc>
  index(documentId: string): Promise<KbDoc>
  search(baseIds: string[], query: string): Promise<KbHit[]>
}

/* One hit. `score` is a similarity, so higher is nearer. */
export interface KbHit {
  score: number
  document_id: string
  text: string
}
