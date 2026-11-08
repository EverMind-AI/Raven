/* Show the reader a thing another island owns.
 *
 * A citation in the transcript names a document in a knowledge base, and
 * pressing it has to open that document at that passage. The transcript cannot
 * reach for the knowledge page to do it: two domains importing each other is
 * what puts every island in every island's closure, and the gate on that
 * (scripts/gates/import-direction.test.mjs) says the fix is to invert the call
 * rather than to pin the edge.
 *
 * So the domain that can do it registers, here, and whoever has a reason calls.
 * Nothing is registered until the island that owns the answer is installed,
 * which is why the verb answers whether it did anything: a transcript restored
 * on a page without the knowledge island still draws its citations, and
 * pressing one does nothing rather than throwing.
 */

/** Where a passage sits, as a citation names it. */
export interface DocumentAt {
  baseId: string
  documentId: string
  /** Which piece of it, in the chunker's own numbering. */
  chunkIndex: number
}

type Opener = (at: DocumentAt) => void

let opener: Opener | null = null

/** The knowledge island, saying it can open one of these. */
export function onDocument(fn: Opener | null): void {
  opener = fn
}

/** Whether anything is registered, for a surface deciding whether to offer it. */
export const canOpenDocument = (): boolean => opener !== null

/** Open one, and say whether anything could. */
export function document(at: DocumentAt): boolean {
  if (!opener) return false
  opener(at)
  return true
}

/** For tests, and for a boot that installs twice. */
export function _resetForTests(): void {
  opener = null
}
