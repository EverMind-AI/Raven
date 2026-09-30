/* What a conversation was pointed at: the knowledge bases its turns may search.
 *
 * The `@` in the composer bar (src/chrome/AtMenu.tsx) offers two things a
 * message can be given besides its words and its attachments: the folder it
 * runs in, which is state/workdir.ts's and is only raised from here, and the
 * bases it may search, which are this module's.
 *
 * The same two-lives shape the folder has. On a draft the pick is held here
 * and spent on `session.create`, because that is the one moment the engine
 * takes one; in a conversation it is written straight through
 * `session.set_knowledge`, because a reader who attaches a base mid-way means
 * the next turn, not the next conversation. A pick never crosses from one
 * conversation to the next: the draft's is dropped with the draft.
 *
 * The list of bases is read once and kept. It is the same read the knowledge
 * page makes, and a menu that refetched every time it opened would spend a
 * round trip to draw a list of three names that has not changed.
 */

import { current } from '../lib/session'
import { gateway } from '../rpc/gateway'
import { makeStore } from './store'

/** One base a conversation can be pointed at. */
export interface MentionBase {
  id: string
  name: string
  /** How many documents are in it, for a reader choosing between two. */
  documents: number
}

export interface MentionState {
  /** Whether the `@` menu is up. */
  readonly open: boolean
  /** Which of the menu's two rows is showing its own list, or null for the
   *  rows themselves. The folder row hands off to the workdir popover, so
   *  only the bases have a panel here. */
  readonly panel: 'bases' | null
  /** The bases there are. Null until they have been read, which is not the
   *  empty list a machine with none answers with. */
  readonly bases: MentionBase[] | null
  /** Ids the conversation is pointed at, in the order they were picked. */
  readonly picked: readonly string[]
  /** Why the list could not be read, as the server's own sentence. */
  readonly failed: string | null
}

const shut: MentionState = { open: false, panel: null, bases: null, picked: [], failed: null }
const store = makeStore<MentionState>(shut)

export const { get, set, subscribe, _resetForTests } = store

const patch = (next: Partial<MentionState>): void => set((prev) => ({ ...prev, ...next }))

/** How many bases this conversation is pointed at. What the chip counts. */
export const count = (): number => get().picked.length

/* The three verbs every popover on this bar has, in the shape the others have
   them (state/plus.ts, state/perm.ts): the chrome's own table reads all four
   through one interface, and a store that answered only to `toggle` would be
   the one that had to be special-cased. */
export function open(): void {
  patch({ open: true, panel: null })
}

export function close(): void {
  patch({ open: false, panel: null })
}

export const isOpen = (): boolean => get().open

/* The button toggles rather than opens: the popover has no close button of its
   own, so the button is the way back out with the pointer. */
export function toggle(): void {
  if (isOpen()) close()
  else open()
}

/* The bases, read once. A failure is kept rather than retried: the menu says
   why, and opening it again is the retry -- a reader who has just fixed the
   gateway should not have to wait out a backoff to see it. */
export async function load(): Promise<void> {
  if (get().bases !== null) return
  try {
    const out = await gateway().call('knowledge.bases.list', {})
    patch({
      bases: (out.bases ?? []).map((row) => ({
        id: row.id,
        name: row.name,
        documents: row.documents ?? 0,
      })),
      failed: null,
    })
  } catch (e) {
    patch({ bases: [], failed: (e as { message?: string })?.message || String(e) })
  }
}

/** Back to the two rows from the bases panel. */
export function openRows(): void {
  patch({ panel: null })
}

export function openBases(): void {
  patch({ panel: 'bases' })
  void load()
}

/* Tick one base, or untick it.

   Written through at once rather than on closing the menu: there is no
   confirm here and nothing that would carry an unsaved pick, so a menu shut
   by a click elsewhere would otherwise lose it. */
export function pick(id: string): void {
  const now = get().picked
  patch({ picked: now.includes(id) ? now.filter((one) => one !== id) : [...now, id] })
  void commit()
}

/** Point the conversation at nothing. */
export function clear(): void {
  if (!get().picked.length) return
  patch({ picked: [] })
  void commit()
}

/* ---- the draft's pick ---------------------------------------------------- */

/** The draft's pick, for the promotion to hand to `session.create`. */
export const staged = (): string[] => [...get().picked]

/* The pick is spent or the draft is gone. The list of bases is kept: it is a
   fact about the machine, not about the conversation that just ended. */
export function clearStaged(): void {
  patch({ picked: [], open: false, panel: null })
}

/* ---- writing it through -------------------------------------------------- */

/* Which conversation is on screen, and whether it exists yet. `null` is a
   draft, whose pick is staged instead.

   The pointer module and not state/session: that is a layer above this one and
   reaching up into it would be a cycle (scripts/gates/import-direction), while
   `lib/session` is the bare pointer every layer reads. Overridable so a test
   can say which conversation is open without standing one up. */
let living: () => string | null = current

export function useSession(reader: () => string | null): void {
  living = reader
}

async function commit(): Promise<void> {
  const id = living()
  if (!id) return
  try {
    await gateway().call('session.set_knowledge', { session_id: id, knowledge_bases: [...get().picked] })
  } catch {
    /* Left as the reader ticked it. The selection is what the panel shows and
       the next turn reads; a write that failed will be retried by the next
       tick, and undoing their click to report a gateway blip would lose the
       one thing they were trying to say. */
  }
}

/** What a conversation already carries, drawn when one is opened. */
export function adopt(ids: readonly string[]): void {
  patch({ picked: [...ids] })
}
