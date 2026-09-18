/* -- the model and the tier: the rpc source ---------------------------
   Which model a conversation runs, which providers this install can reach,
   and which effort rung the next turn takes. The picker itself is the model
   island (ui-web/src/features/model/) and the provider pane is the settings
   island; what is here is the provider list this transport fetched, the two
   writes that can be refused, and the generation ticket that keeps a slow
   answer from repainting a page the reader has left. */

import { t } from '../../i18n/t'
import { current as sessionCurrent } from '../../lib/session'
import { gateway } from '../../rpc/gateway'
import { generation } from '../../state/session/generation'
import { staging } from '../../state/session/staging'
import { open as openSettings, openModels, openProviderModels } from '../settings/store'
import { setCurrent } from './store'

import type { ParamsOf } from '../../rpc/generated'
import type { TierReply, TierSource } from '../../state/tier'
import type { ApiProtocol, ModelSource, Provider } from './types'

/* Providers the page does not offer. Both are the generic "some endpoint of
   your own" row, and the page answers that question twice over without them:
   the runtimes have named rows of their own -- Ollama, LM Studio, GPUStack,
   OpenVINO -- and every row carries an API Host field for pointing a vendor at
   a gateway you run. What was left was a row whose name says nothing about
   what it reaches, next to fifty that do.

   Hidden here rather than dropped from the registry: a section already
   configured under either name keeps loading, keeps being served, and keeps
   routing, and `raven provider list` and the onboarding wizard still offer
   both -- which is also where a vendor Raven carries no spec for is set up. */
export const HIDDEN_PROVIDERS = new Set(['hosted_vllm', 'custom'])

let providersLive: Provider[] = []
let defaultModelLive = ''
let defaultProviderLive = ''

/* Whether first-run setup reported a configured provider. On an object because
   the boot in src/app/boot.ts is what learns the answer. */
export const setupState: { providerConfigured: boolean | null } = { providerConfigured: null }

export const providers = (): Provider[] => providersLive
export const defaultModel = (): string => defaultModelLive
export const defaultProvider = (): string => defaultProviderLive

/* The configured default, kept apart from the visible session's model: the
   settings default-model control shows and edits THIS pair (model AND
   provider), while the composer chip shows whatever the open conversation
   runs. Sharing one value made the settings control display the session's
   model -- and badge the session's provider -- as the default. Written by the
   settings load as well as by the write below, which is why it has a setter. */
export function setDefaultPair(model: string, provider: string): void {
  defaultModelLive = model
  defaultProviderLive = provider
}

/* The composer's model chip is written by id rather than rendered (#modelName /
   #modelChip, features/model/chip.ts), so its painter is published here
   (features/settings/wire.ts) rather than this module reaching into the DOM. */
let paintChip: () => void = () => {}
export function setChipPainter(fn: () => void): void {
  paintChip = fn
}

/* The chip the composer shows and the settings default both write. */
export const showModel = (model: string): void => {
  setCurrent(model)
  paintChip()
}

export function openModelsForMissingProvider(): boolean {
  const connected = providersLive.some((p) => p.on)
  const knownMissing = setupState.providerConfigured === false || providersLive.length > 0
  if (connected || !knownMissing) return false
  void openModels()
  return true
}

export async function loadProviders(sid?: string | null, gen?: number): Promise<void> {
  // The model is per conversation, so ask for the visible one's -- model.options
  // stars the row that conversation actually runs, not agents.defaults. Omit the
  // field when there is no session (boot, a draft): the Wire Schema types it as
  // an optional string, and a serialized null is outside that contract.
  const target = sid !== undefined ? sid : sessionCurrent()
  // Captured here when the caller did not bring one, so every refresh carries a
  // ticket by construction rather than by each call site remembering. A caller
  // whose session was resolved BEFORE its own await must still pass the
  // generation it captured then -- the answer is about that older view, and a
  // ticket taken here would read as current.
  const ticket = gen !== undefined ? gen : generation()
  const mo = await gateway().call('model.options', target ? { session_id: target } : {})
  // model.options does its catalogue work off-thread, so responses can land out
  // of click order. A refresh keyed to a superseded view must not repaint the
  // page the reader has since moved to.
  if (ticket !== generation()) return
  providersLive = (mo.providers || []).filter((p) => !HIDDEN_PROVIDERS.has(p.slug)).map((p) => ({
    id: p.slug, name: p.name, homepage: p.homepage || '', models: p.models || [], on: p.authenticated,
    docs: p.docs || '',
    // What the section actually lists, as against `models` above -- the picker's
    // offer, which folds in a curated shortlist nobody added. The settings page
    // manages the first and the composer chooses from the second.
    configured: p.configured_models || [],
    // What each model is called and what it can do, drawn as the picker's icon
    // row. Keyed by the same id as `models`, so a miss is a model the registry
    // knows nothing about rather than a model with nothing to show.
    labels: p.model_labels || {},
    protocols: p.protocols || {}, protocolOverrides: p.protocol_overrides || {},
    kind: p.auth_type || 'api_key', needsBase: !!p.needs_api_base,
    // Addresses to choose between. A provider that has them is asked which
    // storefront the key came from instead of being handed a host field --
    // the key does not say, and the three are separate accounts.
    platforms: p.platforms || [],
    // Whether this one has a key field: false for an address-only local
    // deployment, true for the local servers that can sit behind a token.
    // Answered by the backend so the pane and the wizard cannot disagree.
    acceptsKey: p.accepts_api_key !== false,
    apiBase: p.api_base || '', defaultApiBase: p.default_api_base || '',
    env: p.key_env || '', warn: p.warning || '',
    key: p.authenticated ? t('gui.set.tls.key_set') : '',
  })) as Provider[]
  if (mo.model) showModel(mo.model)
}

/* provider is required -- a bare model id does not name whose credential serves
   it. scope comes from the opener, matching the TUI's `/model` with or without
   --default:
   - 'default' changes agents.defaults (the settings control). The visible
     session rides along so the server can say whether that conversation follows
     the default; when it does (`applies_to_session`), its chip is re-read, or
     the conversation would run the new default under a chip showing the old.
   - 'session' changes the open conversation; while it is still a draft there is
     no session to scope to, so the pick is staged (writing it would move the
     global default) and applied to the session the first message mints. Staging
     is said back to the caller: it is not an applied switch yet. */
export async function persistModel(
  m: string, provider: string, scope: 'session' | 'default',
): Promise<void | 'staged'> {
  const sid = sessionCurrent()
  // Captured with sid, before the write: the repaint below must prove the page
  // it would paint is still the page the reader is on. config.set and the
  // catalogue read behind model.options are both slow enough for the reader to
  // have left; the generation ticket is what lets loadProviders drop the late
  // answer instead of overwriting the conversation they moved to.
  const gen = generation()
  if (scope === 'default') {
    /* The session rides along only when there is one: the contract types it as
       an optional string, and a serialized null is outside that. */
    const p: ParamsOf<'config.set'> = { key: 'model', value: m, provider, scope: 'default' }
    if (sid) p.session_id = sid
    const r = await gateway().call('config.set', p)
    // Reflect the new default only once it lands, so a refusal leaves the
    // settings row on the pair the config still holds.
    setDefaultPair(m, provider)
    if (r && r.applies_to_session && sid) void loadProviders(sid, gen)
    // A visible draft follows the default the way an unswitched session does:
    // its first message creates the session on the new default, so the chip
    // must move with it -- unless the draft staged a pick of its own, which
    // outranks the default exactly as an own binding does.
    //
    // Under the same generation ticket as the session repaint above, and for
    // the same reason: the picker has closed, nothing locks the write, and
    // leaving the draft for a conversation of its own advances the generation
    // (every view switch does) -- so without this the resolved draft write
    // repaints a chip that has since been loaded correctly for someone else.
    if (!sid && !staging().model && gen === generation()) showModel(m)
    return
  }
  if (sid) {
    await gateway().call('config.set', { key: 'model', value: m, provider, session_id: sid })
    return
  }
  staging().model = { model: m, provider }
  return 'staged'
}

export const modelSource: ModelSource = {
  providers: () => providersLive,
  persist: persistModel,
  setProtocol: async (model: string, provider: string, protocol: ApiProtocol) => {
    await gateway().call('model.set_protocol', { model, slug: provider, protocol })
    await loadProviders()
  },
  openSettings: () => openSettings(),
  openProviderModels: (provider: string) => openProviderModels(provider),
}

/* The tier over the wire. One method serves all three calls, and every reply
   carries the whole catalogue, so `read` and `set` differ only in whether they
   name a mode -- there is no separate menu fetch to keep in step.

   `session_key` is required by the handler and is the conversation the chip sits
   under; a page with no conversation yet has no tier to report, and the caller
   leaves the chip hidden on the refusal rather than inventing one. */
let tierMenu: TierReply['availableModes'] = []

export const tierSource: TierSource = {
  read: async () => {
    /* A draft asks too, and the handler answers the catalogue and its default
       for a key it has never seen -- which is exactly what the first turn of a
       new conversation will run at. */
    const r = await gateway().call('session.set_mode', { session_key: sessionCurrent() || '' })
    tierMenu = (r && r.availableModes) || tierMenu
    return r
  },
  set: async (mode: string) => {
    const sid = sessionCurrent()
    if (!sid) {
      /* Staged, and echoed back as though written: there is no server state to
         contradict it yet, and the chip has to show the reader what their next
         turn will run at. */
      staging().tier = mode
      return { mode, availableModes: tierMenu }
    }
    const r = await gateway().call('session.set_mode', { session_key: sid, mode })
    tierMenu = (r && r.availableModes) || tierMenu
    return r
  },
}

/* Read by the send path, which applies it next to the staged model. The object
   is reset on both paths that abandon a draft. */
export function stagedTier(): string | null {
  const s = staging()
  const mode = s.tier
  s.tier = null
  return mode
}

/* Test seam only: what the gateway last said, and the chip painter the page
   registered, are both the module's. */
export function _resetForTests(): void {
  providersLive = []
  defaultModelLive = ''
  defaultProviderLive = ''
  paintChip = () => {}
  tierMenu = []
}
