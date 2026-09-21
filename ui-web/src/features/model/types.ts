/* The default-model picker's rows and its DataSource contract.
 *
 * One provider carries more than this page shows -- the settings island reads
 * the key state, the auth kind and the warnings off the same objects -- so the
 * shape here is the shared one, and this feature only reads the three fields it
 * draws from.
 */

import type { ModelTagFacts } from '../../components/ModelTags'

export interface Provider {
  id: string
  name: string
  homepage?: string
  keyUrl?: string
  /* Custom request headers by name, each value redacted by the server. */
  headers?: Record<string, string>
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
  /* Resells other vendors' models under vendor/model ids (the registry's
     `is_gateway`). The settings catalogue filters on it. */
  gateway?: boolean
  /* The provider the running conversation's model is served by, as the wire
     marks it (`is_current`). The picker uses it to place that model in the
     right column when the column does not carry it. */
  current?: boolean
  protocols?: Record<string, string>
  protocolOverrides?: Record<string, string>
  /* Keyed by the id as it appears in `models`. Absent for a model the registry
     knows nothing about, which is why every reader treats a miss as "no tags"
     rather than as an empty model. */
  labels?: Record<string, ModelTagFacts & { label?: string; description?: string }>
}

/* The buckets a model list filters by, `registry_data.KINDS`. Ordered as the
   add-model row draws its tabs and as the typed-id chip cycles. */
export type Kind = 'text' | 'image' | 'embedding' | 'reranker' | 'audio' | 'video'
export const KIND_ORDER: readonly Kind[] = ['text', 'image', 'embedding', 'reranker', 'audio', 'video']

/* What ONE opening of the picker lists, and what a pick there means.
 *
 * The composer opens with the default offer -- text models, every connected
 * provider, a pick switches this conversation. A settings role slot opens with
 * its own: its kind, the providers that role may use, its name in the header,
 * the model it holds today, and its own write in place of the switch.
 *
 * The kind narrows each provider's COLUMN, never the provider list: a provider
 * whose key works but whose model list nobody has built yet still has to be
 * visible, or the reader cannot tell it is connected (and a model set by
 * onboarding, never "added", would have nowhere to be marked). */
export interface Offer {
  kind: Kind
  /* Provider ids this opening may list; absent means every connected one. */
  providers?: string[]
  /* The slot's name, drawn in the header; absent for the composer, which also
     selects the below-the-anchor placement and the settings footer's absence. */
  title?: string
  /* The pair this opening marks: the slot's model and the provider serving it.
     Listed in that provider's column even when the column does not carry it.
     Absent means this slot holds nothing yet -- or, for an opening with no
     `pick` (the composer's), the conversation's current model. */
  current?: { model: string; provider: string }
  /* What a pick does. Absent means switch this conversation (`store.choose`).
     `typed` is true for an id the provider does not list yet, and `kind` is
     what the chip beside it was showing -- what the person says that id IS,
     which no catalogue can tell the caller. */
  pick?(model: string, provider: string, typed: boolean, kind: Kind): Promise<void>
}

/* Which bucket a model is in, and what a name alone says it is.

   One classifier, in Python: `registry_data.kind_of` files a model by what it
   WRITES -- reading pictures is something a text model does -- and the answer
   rides on every `model.options` label as `kind`. Nothing here derives one from
   capabilities or modalities; two classifiers eventually disagree and only one
   of them is the one that routes.

   `guessKind` is the exception, and it guesses about a model nobody has
   described: an id a person just typed. It is a translation of
   `registry_data.inferred_tags` -- same two patterns, same last path segment,
   rerank asked first because a reranker is usually named after the embedding
   family it reranks for. `tests/test_provider_registry_data.py` and
   `types.test.ts` assert the same nine rows so the copy cannot drift. */

/* The registry's answer, or text. A model with no label entry is one nothing
   describes, which `kind_of((), ())` also calls text; an unknown string is a
   catalogue newer than this page, and text is the honest fallback. */
export const modelKind = (facts: ModelTagFacts | undefined): Kind =>
  (KIND_ORDER as readonly string[]).includes(facts?.kind ?? '') ? (facts!.kind as Kind) : 'text'

/* One model, however it was spelled. The backend's identity rule is
   `providers/wire.py`'s `merge_key`: strip a leading `<provider>/`, lowercase,
   compare. Two surfaces need it -- a provider's list mixes ids added by hand
   (as typed) with ids the vendor reports (qualified), and a role stores the
   spelling it was given while `model.add_model` stores the one it derived. Both
   drew the same model twice before this. */
export const bareModel = (slug: string, id: string): string =>
  (id.toLowerCase().startsWith(`${slug.toLowerCase()}/`) ? id.slice(slug.length + 1) : id).toLowerCase()

export const sameModel = (slug: string, a: string, b: string): boolean =>
  bareModel(slug, a) === bareModel(slug, b)

/* The next kind in the cycle. Both surfaces that let an id be typed offer the
   same wheel, and the arithmetic was written four times between them. */
export const nextKind = (kind: Kind): Kind =>
  KIND_ORDER[(KIND_ORDER.indexOf(kind) + 1) % KIND_ORDER.length]!

export const KIND_LABEL: Record<Kind, string> = {
  text: 'gui.model.type.text',
  image: 'gui.model.type.image',
  audio: 'gui.model.type.audio',
  video: 'gui.model.type.video',
  embedding: 'gui.model.type.embedding',
  reranker: 'gui.model.type.reranker',
}

/* The sprite id in components/ModelTags.tsx for each kind. The sprite is a
   table of capabilities plus `text`, so it has `embedding` and `rerank` and the
   `*-generation` compounds, and no bare `image` / `audio` / `video` /
   `reranker`: every glyph drawn for a kind goes through here, never through the
   Kind value. */
export const KIND_GLYPH: Record<Kind, string> = {
  text: 'text',
  image: 'image-generation',
  audio: 'audio-generation',
  video: 'video-generation',
  embedding: 'embedding',
  reranker: 'rerank',
}

const RERANK = /rerank/i
const EMBEDDING = /(?:^|[-_/])(?:bge|gte|e5|m3e|text2vec|uae|jina-clip)(?:[-_.]|$)|embed/i

export function guessKind(id: string): Kind {
  const bare = id.split('/').pop() ?? id
  if (RERANK.test(bare)) return 'reranker'
  if (EMBEDDING.test(bare)) return 'embedding'
  return 'text'
}

/* The tags a person states for a typed id, by the kind they named. Text states
   nothing: the backend infers from the name (`registry_data.inferred_tags`),
   and an overlay of empty lists would overwrite a description somebody wrote. */
export const statedTags = (kind: Kind): { capabilities?: string[]; output_modalities?: string[] } => {
  if (kind === 'text') return {}
  const table: Record<Exclude<Kind, 'text'>, [string, string[]]> = {
    image: ['image-generation', ['text', 'image']],
    audio: ['audio-generation', ['audio']],
    video: ['video-generation', ['video']],
    embedding: ['embedding', ['vector']],
    reranker: ['rerank', ['text']],
  }
  const [capability, outputs] = table[kind]
  return { capabilities: [capability], output_modalities: outputs }
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
  persist(m: string, provider: string, scope: 'session' | 'default'): Promise<void | 'staged' | 'needs_restart'>
  /* Add a model to a provider's list. The picker's typed-id row calls it
     before the switch, so a conversation never names a model its provider does
     not list. `kind` states what the person said it is, for an id no catalogue
     describes; absent (text) states nothing and leaves the backend's own
     name-based inference standing. */
  addModel?(m: string, provider: string, kind?: Kind): Promise<void>
  setProtocol?(m: string, provider: string, protocol: ApiProtocol): Promise<void>
  /* The settings door, for the picker's own footer. Only offered when the
     picker was opened from the composer chip, since the settings page opening
     itself is not a way out of it. */
  openSettings(): void
  openProviderModels?(provider: string): void
}
