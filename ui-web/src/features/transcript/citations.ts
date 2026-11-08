/* What a knowledge search found, kept beside the call that found it.
 *
 * The tool's result text names the document and the page, which is what the
 * model needs and what a reader can read -- and cannot follow. A citation has
 * to be pressable, and pressing it needs ids the prose does not carry.
 *
 * So the tool files them (raven/agent/tools/knowledge.py) and they ride the
 * same `metadata` channel `deliver_files` uses. Kept in a registry keyed by
 * the call rather than threaded through the call row: the row's shape is the
 * transcript's own, every surface that builds one would have to learn the new
 * field, and nothing but the detail card ever reads this.
 *
 * Bounded, and dropped oldest first. A long conversation can run hundreds of
 * searches, and what a reader can press is what is on screen.
 */

/** One passage a search found, as a citation names it. */
export interface Citation {
  baseId: string
  documentId: string
  /** The document's own name, for the row's label. */
  source: string
  /** The page it starts on, where the format has pages. */
  page: number | null
  chunkIndex: number
}

/** How many calls' citations are kept. Past it, the oldest go. */
const MAX_CALLS = 200

const held = new Map<string, Citation[]>()

function readOne(raw: unknown): Citation | null {
  if (!raw || typeof raw !== 'object') return null
  const row = raw as Record<string, unknown>
  const documentId = typeof row.document_id === 'string' ? row.document_id : ''
  if (!documentId) return null
  return {
    baseId: typeof row.base_id === 'string' ? row.base_id : '',
    documentId,
    source: typeof row.source === 'string' ? row.source : documentId,
    page: typeof row.page === 'number' ? row.page : null,
    chunkIndex: typeof row.chunk_index === 'number' ? row.chunk_index : 0,
  }
}

/* Reads only its own key. The channel carries whatever any tool filed --
   `deliver_files` puts a manifest on it -- so a payload that is not this one's
   is not this one's business. */
export function record(callId: string | null | undefined, metadata: unknown): void {
  const id = String(callId || '')
  if (!id || !metadata || typeof metadata !== 'object') return
  const raw = (metadata as Record<string, unknown>).knowledge_hits
  if (!Array.isArray(raw)) return
  const rows = raw.map(readOne).filter((row): row is Citation => row !== null)
  if (!rows.length) return
  held.set(id, rows)
  while (held.size > MAX_CALLS) {
    const oldest = held.keys().next()
    if (oldest.done) break
    held.delete(oldest.value)
  }
}

/** What one call found, or nothing where it found nothing. */
export function of(callId: string | null | undefined): Citation[] {
  return held.get(String(callId || '')) ?? []
}

export function _resetForTests(): void {
  held.clear()
}
