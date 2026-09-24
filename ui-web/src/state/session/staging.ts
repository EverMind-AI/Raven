/* What a draft chose before it had a conversation to choose it for.
 *
 * A model, a tier and a permission mode picked while the page is still a draft
 * cannot be written: there is no session to scope any of the three to, and
 * writing them would move the global default instead. They are held until the
 * first message mints a session, applied to it, and forgotten.
 *
 * They are the draft's own -- a field of its `SessionRuntime` -- so leaving the
 * draft drops them with it rather than by being cleared on each of the two
 * paths out, and a pick can no longer cross from one conversation to another.
 * This is the view the model and settings sources read and write them through,
 * so neither has to know about the registry.
 */

import { viewRuntime } from './registry'

export interface StagedModel {
  model: string
  provider: string
}

export interface Staging {
  model: StagedModel | null
  tier: string | null
  perm: string | null
}

/** The staged picks of the conversation on screen. */
export const staging = (): Staging => viewRuntime().staged
