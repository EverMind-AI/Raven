/* The Harness a draft will run as: the Persona a reader picked off the wall,
 * held until the first message mints a session.
 *
 * A module beside state/workdir.ts and for its reason: `session.create` is the
 * only moment the engine can freeze a Harness onto a conversation
 * (raven/rpc/methods/session.py), so the pick has to survive from the click on
 * the wall to the promotion, and it cannot live on the draft runtime --
 * state/session/registry.ts imports the rail's store, and a pick that lived
 * there would close that ring.
 *
 * Nothing here opens a conversation. The wall stages a name and leaves the
 * reader on a fresh draft; a wall that minted a session per click would leave
 * an empty conversation behind every time a reader changed their mind.
 */

/* The Persona the draft chose, or null for an ordinary conversation. Consumed
   by the promotion (state/session/runtime.ts) and dropped with the draft, so a
   pick never crosses from one conversation to the next. */
let picked: string | null = null

/** The draft's pick, for the promotion to hand to `session.create`. */
export const staged = (): string | null => picked

/** Hold a Persona for the draft on screen. */
export function stage(name: string | null): void {
  picked = name || null
}

/** The pick is spent, or the draft it belonged to is gone. */
export function clearStaged(): void {
  picked = null
}

/** Back to the first value, for a test that staged a pick. */
export function _resetForTests(): void {
  picked = null
}
