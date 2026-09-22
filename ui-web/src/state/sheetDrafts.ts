/* What the reader has filled in on a sheet that is still waiting for them.
 *
 * The clarify question's form: which step of a batch they are on, and per step
 * what they picked, what they typed and whether they skipped it. It used to be
 * the free-text row alone, and it used to live only in the DOM, kept alive by
 * the rack detaching a parked sheet rather than destroying it -- the half-typed
 * answer survived a conversation switch because the input element itself did.
 * Rendering a sheet from a component takes that away: the interior is unmounted
 * while another conversation is open, so the form has to be somewhere that is
 * not the document. (The approval sheet kept a note and a prefix here once; it
 * answers at once now and types nothing before the answer.)
 *
 * Keyed by the conversation AND the question, not by the conversation alone. At
 * most one of these sheets is pending per conversation (the class sweep on the
 * way in, features/composer/clarify.ts), so a key per conversation would fit --
 * right up to the moment a new question replaces the pending one, where it
 * would hand the new question the retired one's draft. A request with no id of
 * its own -- a frame that predates the field -- falls back to one slot per
 * conversation for that reason, which is the same slot the replacement would
 * take; the sheet that leaves clears it (`forget`), so the replacement still
 * starts empty.
 *
 * The store is not reactive: the field it feeds is uncontrolled, so nothing
 * re-renders when a key is pressed. It is read once when a sheet's interior
 * mounts and written on every keystroke and every pick.
 */

/** One question of a batch, as the reader has left it. */
export interface StepDraft {
  /** The options chosen: one on a single-select step, any number on a multi. */
  picked?: string[]
  /** The free-text row. */
  text?: string
  skipped?: boolean
}

export interface SheetDraft {
  /** Which step the reader is on, and the furthest one they have reached. */
  step?: number
  reached?: number
  steps?: StepDraft[]
}

const DRAFTS = new Map<string, SheetDraft>()

const EMPTY: SheetDraft = {}

/** The key a sheet's draft is filed under: its conversation and its question. */
export const slot = (owner: string, id?: string): string => `${owner}|${id || '(anon)'}`

/** What is filled in so far, or an empty draft for a sheet nobody has touched. */
export const read = (key: string): SheetDraft => DRAFTS.get(key) || EMPTY

/** Records one field; the others keep whatever they held. */
export function write(key: string, patch: SheetDraft): void {
  DRAFTS.set(key, { ...read(key), ...patch })
}

/* Called when a sheet leaves the rack for good -- answered, replaced, or
   dropped with its conversation -- and never on a conversation switch, which is
   the case this store exists for. */
export function forget(key: string): void {
  DRAFTS.delete(key)
}

/* Test seam only: the Map outlives a test file's DOM. */
export function _resetForTests(): void {
  DRAFTS.clear()
}
