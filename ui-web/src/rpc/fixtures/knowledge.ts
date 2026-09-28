/* The knowledge bases, which this page has none of.
 *
 * The one domain the offline shell never had a fixture for: `sources.knowledge`
 * was the live layer's alone, so opening the page with no gateway reached for a
 * source that was not there. It answers an empty library now -- which is what
 * a fresh install has, and a page the reader can look at rather than a failure.
 *
 * The writes answer too, because the page calls them and a missing responder is
 * a page that throws where a reader pressed a button. What they answer is what
 * the server would have: the row as it now stands. Every time field comes off
 * `env.now` rather than `Date.now`, so the library answers byte-identically
 * twice over on a fixed clock (scripts/gates/fixture-now.test.mjs).
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'

/** The base every write answers with, as the contract declares it. */
type Base = ResultOf<'knowledge.bases.create'>['base']

/** The document a reindex answers with, as the contract declares it. */
type Doc = ResultOf<'knowledge.documents.index'>['document']

/** The piece a write answers with, as the contract declares it. */
type Chunk = ResultOf<'knowledge.chunks.create'>['chunk']

/** The folder a write answers with, as the contract declares it. */
type Folder = ResultOf<'knowledge.folders.create'>['folder']

export interface KnowledgeFixture {
  fixtures: Fixtures
}

/** One base, as both writes echo it. The offline page holds no library, so a
 *  write has nothing to look up and answers from what it was sent. */
function baseFrom(params: unknown, env: FixtureEnv): Base {
  const p = (params ?? {}) as {
    base_id?: string
    name?: string
    description?: string
    pinned?: boolean
    starred?: boolean
  }
  const at = new Date(env.now()).toISOString()
  return {
    id: p.base_id ?? `kb-${env.now()}`,
    name: p.name ?? '',
    description: p.description ?? '',
    embedding_model: '',
    embedding_provider: '',
    dimensions: 0,
    created_at: at,
    updated_at: at,
    documents: 0,
    chunks: 0,
    pinned: !!p.pinned,
    starred: !!p.starred,
    /* The tuning the record defaults to. Sent rather than left out because
       scripts/gates/fixture-shape.test.mjs holds the library to the whole
       shape: a field the offline page never sees is a field its renderer is
       never exercised against. */
    top_k: 6,
    smart_chunking: true,
    separator: '\n\n',
    chunk_size: 2048,
    chunk_overlap: 0,
    table_context_size: 64,
    image_context_size: 64,
    file_processing: '',
    embedding_reach: '',
  }
}

/** One document, for the writes that answer with a row. */
function docFrom(params: unknown, env: FixtureEnv): Doc {
  const p = (params ?? {}) as {
    document_id?: string
    base_id?: string
    folder_id?: string
    path?: string
    url?: string
    title?: string
  }
  const at = new Date(env.now()).toISOString()
  return {
    id: p.document_id ?? `doc-${env.now()}`,
    base_id: p.base_id ?? '',
    source: p.path ?? p.url ?? p.title ?? '',
    media_type: '',
    size: 0,
    status: 'ready',
    chunk_count: 0,
    error: '',
    warning: '',
    created_at: at,
    updated_at: at,
    origin: 'file',
    origin_ref: '',
    folder_id: p.folder_id ?? '',
  }
}

/** One folder, for the writes that answer with a row. */
function folderFrom(params: unknown, env: FixtureEnv): Folder {
  const p = (params ?? {}) as { folder_id?: string; base_id?: string; name?: string }
  return {
    id: p.folder_id ?? `f-${env.now()}`,
    base_id: p.base_id ?? '',
    name: p.name ?? '',
    created_at: new Date(env.now()).toISOString(),
    documents: 0,
  }
}

/** The pieces a batch names, as the two batch methods take them. */
function idsOf(params: unknown): string[] {
  return ((params ?? {}) as { chunk_ids?: string[] }).chunk_ids ?? []
}

/** One piece, for the two writes that answer with a row. */
function chunkFrom(params: unknown, env: FixtureEnv): Chunk {
  const p = (params ?? {}) as { chunk_id?: string; text?: string }
  return {
    chunk_index: 0,
    total_chunks: 1,
    text: p.text ?? '',
    layout_type: '',
    heading_path: [],
    parts: [],
    chunk_id: p.chunk_id ?? `c-${env.now()}`,
    enabled: true,
    /* Written by a person either way: both methods that answer with a row are
       a person putting words in one. */
    manual: true,
    has_crop: false,
    regions: [],
  }
}

export function createKnowledge(env: FixtureEnv): KnowledgeFixture {
  return {
    fixtures: {
      /* `provider` rides with the model: a base is embedded through the
         provider that serves it, and a model id does not name one. */
      'knowledge.status': () => ({ configured: false, model: '', provider: '' }),
      'knowledge.bases.list': () => ({ bases: [] }),
      'knowledge.bases.create': (p) => ({ base: baseFrom(p, env) }),
      'knowledge.bases.rename': (p) => ({ base: baseFrom(p, env) }),
      'knowledge.bases.settings': (p) => ({ base: baseFrom(p, env) }),
      'knowledge.bases.delete': () => ({ removed: true }),
      'knowledge.documents.list': () => ({ documents: [] }),
      /* No library, so no document has pieces. `total` rides along because the
         list is paged and a reader of this fixture should see the shape it
         would page through. */
      'knowledge.documents.chunks': () => ({ chunks: [], total: 0 }),
      /* No document, so no pages. The panel reads an empty list as a format
         with none and frames the file instead, which is what it does offline
         for every format anyway. */
      'knowledge.documents.pages': () => ({ pages: [] }),
      /* The four writes a reader makes to one piece. Nothing behind them to
         change, so each answers what the server would: how many it touched,
         or the row as it now stands. */
      'knowledge.chunks.switch': (p) => ({ changed: idsOf(p).length }),
      'knowledge.chunks.delete': () => ({ remaining: 0 }),
      'knowledge.chunks.create': (p) => ({ chunk: chunkFrom(p, env) }),
      'knowledge.chunks.update': (p) => ({ chunk: chunkFrom(p, env) }),
      /* The three ways a document arrives all answer the row they made. What
         the offline page does with it is show it: there is no library behind
         these, so the next list call answers empty again. */
      'knowledge.documents.add': (p) => ({ document: docFrom(p, env) }),
      'knowledge.documents.add_url': (p) => ({ document: docFrom(p, env) }),
      'knowledge.documents.add_note': (p) => ({ document: docFrom(p, env) }),
      /* The offline page holds no library, so reindexing answers a row shaped
         like the one the server would return rather than looking one up. */
      'knowledge.documents.index': (p) => ({ document: docFrom(p, env) }),
      'knowledge.documents.delete': () => ({ removed: true }),
      'knowledge.documents.move': (p) => ({ document: docFrom(p, env) }),
      'knowledge.folders.list': () => ({ folders: [] }),
      'knowledge.folders.create': (p) => ({ folder: folderFrom(p, env) }),
      'knowledge.folders.rename': (p) => ({ folder: folderFrom(p, env) }),
      'knowledge.folders.delete': () => ({ moved: 0 }),
      /* `by_keyword` is what a search fell back to when the embedding
         endpoint could not be reached, so a reader can tell a keyword hit
         from a semantic one. Empty here, like the hits it accompanies. */
      'knowledge.search': () => ({ hits: [], by_keyword: [] }),
    },
  }
}
