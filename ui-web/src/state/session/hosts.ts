/* The transcript lane hosts conversations are holding off screen.
 *
 * A host detached from `#stage` is not finished with while the conversation it
 * belongs to still has a turn streaming into its lane -- the streamed tokens
 * live in that lane and nowhere else until the turn ends. So the transcript
 * island asks before it drops a detached one (features/transcript/mount.tsx),
 * and this is what it asks: the residency rule, as a set of elements.
 *
 * A module with no imports of its own, so the island's renderer answers the
 * question without pulling the session layer in behind it. The runtime is
 * still the owner -- `SessionRuntime.host` is the handle -- and residency is
 * the only writer here.
 */

const held = new Set<HTMLElement>()

export const hold = (node: HTMLElement): void => { held.add(node) }

export const drop = (node: HTMLElement): void => { held.delete(node) }

export const dropAll = (): void => { held.clear() }

/** Whether any conversation is holding this lane host. */
export const holdsHost = (node: HTMLElement): boolean => held.has(node)
