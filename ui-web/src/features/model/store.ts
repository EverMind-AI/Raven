/* The default-model picker: what is open, and what picking one means.
 *
 * The popover is one node over the whole page rather than a page's own root, so
 * a single React root lives at the body and renders nothing while the picker is
 * closed. Opening is a call, not a route: the composer's chip and the settings
 * island both ask for it, and the settings island passes a callback because it
 * paints the chosen model in its own tree.
 */

import { t } from '../../i18n/t'
import { ds } from '../../state/sources'
import { sources } from '../../state/sources'
import { show as toast } from '../../state/toast'
import { KIND_ORDER, modelKind, offered, statedTags } from './types'

import type { ApiProtocol, Kind, ModelSource, Offer, Provider } from './types'

export interface OpenAt {
  /* What the popover is anchored to and must not close on a click inside.
     Null while closed. */
  host: HTMLElement | null
  /* Called after every local change to the pick, forward or rolled back, so a
     caller painting the model in its own tree stays in step. */
  after: (() => void) | null
  /* The footer only appears for the composer chip. The settings island's own
     button is already on the settings page. */
  footer: boolean
  /* Which switch the opener meant: the composer chip changes THIS conversation
     ('session'), the settings default-model control changes what new ones start
     on ('default'). The backend needs it explicitly -- a model id alone does not
     say -- and inferring it from whether a conversation happens to be open is
     exactly the bug that let the settings control change one session. */
  scope: 'session' | 'default'
  /* Which model the picker marks, when it is not the one the chip shows. The
     chip and the picker share one `current` (the open conversation's model), so
     the settings control -- which edits the default, a different value -- passes
     it here to mark the right row without moving the chip. Null means mark
     `current`. */
  marked: string | null
  /* What this opening lists and what a pick means: see `Offer` in types.ts. */
  offer: Offer
}

/* Text models, every connected provider, a pick switches this conversation:
   the composer chip's opening, and what any caller that names nothing gets. */
const DEFAULT_OFFER: Offer = { kind: 'text' }

const CLOSED: OpenAt = { host: null, after: null, footer: false, scope: 'session', marked: null, offer: DEFAULT_OFFER }

let at: OpenAt = CLOSED
let epoch = 0
let selected = 'minimax-m3'
const subs = new Set<() => void>()

export const source = (): ModelSource => ds('model')

/* Installed by the live layer only. The offline demo's chip opens a plain menu
   of its own (the composer's model button), so the opener below has to be callable and
   do nothing there rather than throw at the name the chrome imports. */
const installed = (): boolean => !!sources.model

export function subscribe(fn: () => void): () => void {
  subs.add(fn)
  return () => subs.delete(fn)
}

const announce = (): void => {
  epoch += 1
  subs.forEach((fn) => fn())
}

export const openAt = (): OpenAt => at
export const version = (): number => epoch
export const isOpen = (): boolean => !!at.host
export const current = (): string => selected

export function setCurrent(model: string): void {
  if (selected === model) return
  selected = model
  announce()
}

/* Every open replaces the one before it rather than stacking: the chip and the
   settings button can both be reached while a picker is up. */
export function open(anchor?: HTMLElement | null, after?: () => void, marked?: string, offer: Offer = DEFAULT_OFFER): void {
  if (!installed()) return
  const rows = listed(offer)
  if (!rows.length) {
    /* Nothing to open onto is now one dead end, not two: a provider connected
       with nothing added is listed, with a column that says so and a row to
       type an id into, so "go and build a list" is answered inside the picker.
       What is left is having no account the offer allows -- a credential to go
       and add, or, for a slot restricted to one vendor, that vendor. */
    const only = offer.providers?.length === 1
      ? source().providers().find((p) => p.id === offer.providers![0])
      : undefined
    if (only && !only.on) {
      source().openProviderModels?.(only.id)
      toast(t('gui.picker.no_models_for', { name: only.name }))
    } else {
      toast(t('gui.picker.no_account'))
    }
    return
  }
  const host = anchor || document.getElementById('modelChip')
  if (!host) return
  /* The composer chip opens with no anchor and means this conversation; the
     settings control passes its button and means the default, and marks the
     default model rather than the chip's. */
  at = { host, after: after || null, footer: !anchor, scope: anchor ? 'default' : 'session', marked: marked || null, offer }
  announce()
}

export function close(): void {
  if (!at.host) return
  at = CLOSED
  announce()
}

/* The providers one opening lists: connected, and named by the offer when it
   names any. The kind does NOT filter here -- it narrows each provider's column
   (`column` below) instead.
   A provider whose key works but whose model list nobody has built yet stays
   visible: hiding it told a reader with a working DeepSeek key that DeepSeek
   was not connected (reported 2026-09-20), and the model the chip named -- set
   by onboarding, never "added" -- had no column to be marked in. */
export const listed = (offer: Offer = at.offer): Provider[] =>
  source()
    .providers()
    .filter((p) => p.on && (!offer.providers || offer.providers.includes(p.id)))

/* One provider's column: its models of the offer's kind, with the current one
   first when the list does not carry it. `agents.defaults.model` is routinely
   set by onboarding or the CLI without a matching entry under
   `providers.<slug>.models`, and a model the page is showing as current has to
   be somewhere the reader can see it marked. */
export const column = (p: Provider, offer: Offer = at.offer): string[] => {
  const rows = offered(p).filter((m) => modelKind(p.labels?.[m]) === offer.kind)
  /* Whose model to pin. A slot states its own pair or holds none -- an unset
     slot has nothing to pin, and borrowing the conversation's model would put
     a chat model at the top of the embedding column. Only the composer falls
     back to the conversation's, in the provider the wire marks as serving it.
     `pick` is what tells them apart: every slot writes through one. */
  const cur = offer.current
    ? (offer.current.provider === p.id ? offer.current.model : null)
    : (offer.pick ? null : (p.current ? selected : null))
  return cur && !rows.includes(cur) ? [cur, ...rows] : rows
}

export { KIND_ORDER, statedTags }

export const protocolFor = (provider: Provider, model: string): ApiProtocol => {
  const configured = provider.protocols?.[model]
  return configured === 'chat' || configured === 'responses' || configured === 'anthropic' ? configured : 'chat'
}

export async function setProtocol(model: string, provider: string, protocol: ApiProtocol): Promise<void> {
  const setter = source().setProtocol
  if (!setter) throw new Error(t('gui.model.protocol_unsupported'))
  await setter(model, provider, protocol)
  announce()
}

/* Optimistic: the chip has to say the new model before the round trip, because
   the next turn already uses it. A rejected write puts the old one back and
   says so rather than leaving the page claiming a model the config never took. */
export async function choose(m: string, provider: string, typed = false, kind?: Kind): Promise<void> {
  const src = source()
  const prev = current()
  const after = at.after
  const scope = at.scope
  const { offer } = at
  close()
  /* A slot writes through its own path -- a role key, an EverOS section, a
     media pair -- and never through the conversation switch below, whose scope
     logic answers a different question. Returned before it: the two are
     alternatives, not steps. */
  if (offer.pick) {
    try {
      await offer.pick(m, provider, typed, kind ?? offer.kind)
    } catch (e) {
      toast(t('gui.op.switch_failed', { detail: detail(e) }))
    }
    return
  }
  /* The typed id joins the provider's list before the switch: a conversation
     must not name a model its provider does not list. */
  if (typed) {
    try {
      /* The kind the chip was showing when it was clicked, which is what the
         person said this id is -- not the offer's, which is only its default. */
      await src.addModel?.(m, provider, kind ?? offer.kind)
    } catch (e) {
      toast(t('gui.op.switch_failed', { detail: detail(e) }))
      return
    }
  }
  /* A session switch is optimistic: the chip -- which reads the same `current`
     -- says the new model before the round trip, and rolls back if it is
     refused. A default switch must not touch the chip (a different value), so it
     commits nothing locally and only reflects the settled default through
     `after` once the write lands. */
  if (scope === 'session') {
    setCurrent(m)
    after?.()
  }
  try {
    const settled = await src.persist(m, provider, scope)
    if (scope === 'default') after?.()
    /* A staged pick (a draft, applied when its session is created) is not an
       applied switch, and saying so here is what keeps a later refusal from
       contradicting an earlier success claim. */
    toast(settled === 'staged'
      ? t('gui.model.pick_staged', { name: short(m) })
      : t('gui.model.pick_switched', { name: short(m) }))
  } catch (e) {
    if (scope === 'session') {
      setCurrent(prev)
      after?.()
    }
    toast(t('gui.op.switch_failed', { detail: detail(e) }))
  }
}

/* Provider-qualified names arrive as `vendor/model`; the page has never shown
   the vendor half, which the provider column already says. */
export const short = (m: string): string => String(m || '').split('/').pop() as string

const detail = (e: unknown): string => {
  const err = e as { data?: { detail?: string }; message?: string } | null
  return err?.data?.detail || err?.message || String(e)
}

export function _resetForTests(): void {
  at = CLOSED
  epoch = 0
  selected = 'minimax-m3'
  subs.clear()
}
