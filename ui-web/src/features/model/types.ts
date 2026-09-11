/* The default-model picker's rows and its DataSource contract.
 *
 * One provider carries more than this page shows -- the settings island reads
 * the key state, the auth kind and the warnings off the same objects -- so the
 * shape here is the shared one, and this feature only reads the three fields it
 * draws from.
 */

import type { ModelTagFacts } from '../../shell/model-tags'

export interface Provider {
  id: string
  name: string
  homepage?: string
  /* Everything this provider could serve: its configured list plus a curated
     shortlist plus a catalogue. The picker does not offer this -- see
     `offered` below -- but the onboarding step, which runs before anything has
     been added, does. */
  models: string[]
  /* What was actually added to this provider, which is what the picker offers.
     A model is chosen from the list somebody built in settings, not from
     everything the vendor has ever published. */
  configured?: string[]
  /* Authenticated. A provider without an account is not offered: picking one of
     its models would fail on the next turn rather than at the click. */
  on: boolean
  kind?: string
  protocols?: Record<string, string>
  protocolOverrides?: Record<string, string>
  /* Keyed by the id as it appears in `models`. Absent for a model the registry
     knows nothing about, which is why every reader treats a miss as "no tags"
     rather than as an empty model. */
  labels?: Record<string, ModelTagFacts & { label?: string; description?: string }>
}

export type ApiProtocol = 'auto' | 'chat' | 'responses' | 'anthropic'

/* The models a picker offers for a provider: the added ones. Older sources
   that predate the split hand back only `models`, and falling through to it
   keeps them working rather than emptying their picker. */
export const offered = (p: Provider): string[] => p.configured ?? p.models

export interface ModelSource {
  providers(): Provider[]
  /* Send it. The provider is required: a model id does not name whose
     credential serves it, so the backend refuses a switch without one -- the
     column the model was chosen from is that answer. ``scope`` says whether the
     switch is for this conversation or the global default, carried from the
     opener rather than guessed from whether a conversation is open. Rejecting is
     meaningful: the picker rolls the local pick back. Resolving to ``'staged'``
     means the pick was held rather than applied (a draft has no session yet),
     and the picker words its toast accordingly. */
  persist(m: string, provider: string, scope: 'session' | 'default'): Promise<void | 'staged'>
  setProtocol?(m: string, provider: string, protocol: ApiProtocol): Promise<void>
  /* The settings door, for the picker's own footer. Only offered when the
     picker was opened from the composer chip, since the settings page opening
     itself is not a way out of it. */
  openSettings(): void
}
