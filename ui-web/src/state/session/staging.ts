/* What a draft chose before it had a conversation to choose it for.
 *
 * A model, a tier and a permission mode picked while the page is still a draft
 * cannot be written: there is no session to scope any of the three to, and
 * writing them would move the global default instead. They are held until the
 * first message mints a session, applied to it, and forgotten -- and cleared on
 * either path that abandons the draft, so a stale pick cannot land on the next
 * conversation.
 *
 * The object itself is still the page's (src/legacy/live/080-overrides.js),
 * which is where those two reset paths are; this is the interface the sources
 * read and write it through, so neither has to import the other.
 */

export interface StagedModel {
  model: string
  provider: string
}

export interface Staging {
  model: StagedModel | null
  tier: string | null
  perm: string | null
}

let current: Staging = { model: null, tier: null, perm: null }

/** The page publishes the object it resets. */
export function setStaging(next: Staging): void {
  current = next
}

export function staging(): Staging {
  return current
}

/** Back to a fresh page's staging. For tests. */
export function resetStaging(): void {
  current = { model: null, tier: null, perm: null }
}
