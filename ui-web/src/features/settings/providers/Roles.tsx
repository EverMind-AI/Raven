/* The model roles: which model each job runs on, and the picker that changes
   it. Eleven rows; each knows where its pair is written and which providers
   may serve it. The media tools' rows on the Tools page draw the same pill. */
import { useRef } from 'react'

import { t } from '../../../i18n/t'
import { openPicker } from '../../model/source'
import { statedTags } from '../../model/types'
import { Card, Row, Rov, Seg, Stepper } from '../Fields'
import * as store from '../store'

import type { Kind, Offer } from '../../model/types'
import type { ProviderRow, SettingsSnapshot } from '../types'
import type { JSX } from 'react'

export type RoleId =
  | 'chat' | 'curator' | 'title' | 'memllm' | 'gate'
  | 'embedding' | 'rerank' | 'multimodal' | 'image' | 'speech' | 'video'

/* What each slot may be filled with. The one consumer of a model's kind: an
   embedding slot listing a chat model is a pick that fails on the next call,
   and the slot is the only place that knows which it wants.
   Text for every slot that talks: `kind_of` files a model by what it WRITES, so
   a model that also reads pictures is text, and the multimodal slot -- which
   wants exactly that -- takes text too. */
export const ROLE_KIND: Record<RoleId, Kind> = {
  chat: 'text', curator: 'text', title: 'text', memllm: 'text', gate: 'text', multimodal: 'text',
  embedding: 'embedding', rerank: 'reranker', image: 'image', speech: 'audio', video: 'video',
}

export interface Role {
  id: RoleId
  /* settings.set keys for the model and the provider. */
  keys?: [string, string]
  /* An EverOS section, written through settings.everosSet. */
  everos?: string
  /* tools.media.<kind>, and the tool that exists to run it. */
  media?: string
  tool?: string
  optional?: boolean
}

export const ROLES: Role[] = [
  { id: 'chat' },
  { id: 'curator', keys: ['context.curatorModel', 'context.curatorProvider'] },
  { id: 'title', keys: ['sessionTitle.model', 'sessionTitle.provider'] },
  { id: 'memllm', everos: 'llm' },
  { id: 'gate', keys: ['skillForge.llmGateModel', 'skillForge.llmGateProvider'] },
  { id: 'embedding', everos: 'embedding', optional: true },
  { id: 'rerank', everos: 'rerank', optional: true },
  { id: 'multimodal', everos: 'multimodal', optional: true },
  { id: 'image', media: 'image', tool: 'image_generate', optional: true },
  { id: 'speech', media: 'speech', tool: 'text_to_speech', optional: true },
  { id: 'video', media: 'video', tool: 'video_generate', optional: true },
]

/* Literal keys, for the i18n gate. */
const NAME: Record<RoleId, string> = {
  chat: 'gui.settings.roles.chat', curator: 'gui.settings.roles.curator', title: 'gui.settings.roles.title',
  memllm: 'gui.settings.roles.memllm', gate: 'gui.settings.roles.gate', embedding: 'gui.settings.roles.embedding',
  rerank: 'gui.settings.roles.rerank', multimodal: 'gui.settings.roles.multimodal', image: 'gui.settings.roles.image',
  speech: 'gui.settings.roles.speech', video: 'gui.settings.roles.video',
}
const USE: Record<RoleId, string> = {
  chat: 'gui.settings.roles.chat_use', curator: 'gui.settings.roles.curator_use', title: 'gui.settings.roles.title_use',
  memllm: 'gui.settings.roles.memllm_use', gate: 'gui.settings.roles.gate_use', embedding: 'gui.settings.roles.embedding_use',
  rerank: 'gui.settings.roles.rerank_use', multimodal: 'gui.settings.roles.multimodal_use', image: 'gui.settings.roles.image_use',
  speech: 'gui.settings.roles.speech_use', video: 'gui.settings.roles.video_use',
}
export const roleName = (r: Role): string => t(NAME[r.id])

const MEDIA_PROVIDER = 'openrouter'
const CTX_MIN = 1024
const ITER_RANGE: [number, number] = [1, 200]

const dig = (raw: Record<string, unknown>, path: string): unknown =>
  path.split('.').reduce<unknown>((o, k) => (o && typeof o === 'object' ? (o as Record<string, unknown>)[k] : undefined), raw)

export const disabledTools = (raw: Record<string, unknown>): string[] => (dig(raw, 'tools.disabledTools') as string[] | undefined) || []

export interface RoleValue {
  model: string
  provider: string
}

/* The pair a role is set to, or null for "follows the chat model" / not set. */
export function roleValue(r: Role, snap: SettingsSnapshot): RoleValue | null {
  if (r.id === 'chat') return snap.model ? { model: snap.model, provider: snap.curProvider } : null
  if (r.keys) {
    const model = dig(snap.raw, r.keys[0])
    if (typeof model !== 'string' || !model) return null
    const provider = dig(snap.raw, r.keys[1])
    return { model, provider: typeof provider === 'string' ? provider : '' }
  }
  if (r.everos) {
    const sec = snap.everos?.sections?.[r.everos]
    if (!sec || !sec.model) return null
    return { model: sec.model, provider: sec.provider || '' }
  }
  if (r.media) {
    const sel = dig(snap.raw, `tools.media.${r.media}`) as { model?: string } | undefined
    return sel && sel.model ? { model: sel.model, provider: MEDIA_PROVIDER } : null
  }
  return null
}

/* Which connected providers may serve a role: media runs on OpenRouter, an
   EverOS role takes whichever vendors can actually serve it, the rest take any.

   Capability, not auth shape. The filter used to ask `kind === 'key'`, which is
   how the rerank slot came to offer OpenAI -- and, once a self-hosted endpoint
   became an ordinary vendor here, how every one of them disappeared: they are
   `local` and `endpoint`, not `key`. Whether a vendor holds a usable credential
   is a separate question, refused at the save with a sentence naming it. */
export function roleProviders(r: Role, snap: SettingsSnapshot): ProviderRow[] {
  const on = snap.providers.filter((p) => p.on)
  if (r.media) return on.filter((p) => p.id === MEDIA_PROVIDER)
  if (r.everos) {
    if (snap.everos?.available === false) return []
    const role = r.everos
    /* A model the vendor serves is the vendor's to say, and a catalogue is not
       the last word on it: any connected provider may be picked, with an id
       typed if its list lacks one. Rerank is different in kind -- EverOS has
       to build a request in that vendor's own rerank shape, which no typed id
       supplies -- so it keeps to the vendors whose shape is known. */
    if (role !== 'rerank') return on
    const offered = on.filter((p) => (snap.everos?.supports?.[p.id] || []).includes(role))
    /* Reranking against somebody's own box needs a request shape the vendor
       table cannot name, and this page has nowhere to ask for one -- a first
       save of such a provider is refused, so offering it here would be a picker
       entry whose only outcome is an error. The wizard asks, so one already
       configured stays pickable and its model stays editable. */
    const pinned = snap.everos?.sections?.rerank?.provider
    return offered.filter((p) => !isSelfHost(snap, p.id) || p.id === pinned)
  }
  return on
}

/* A vendor whose rerank request shape nothing but its operator knows: raven
   carries no table entry naming it, which is exactly the self-hosted case. */
function isSelfHost(snap: SettingsSnapshot, id: string): boolean {
  const p = snap.providers.find((x) => x.id === id)
  return !!p && (p.kind === 'local' || p.id === 'custom')
}

/* Whether this role's slot can be edited at all. A root the user manages is
   raven's to read and not to write -- it neither starts nor configures it --
   and a role set from exported variables outranks anything saved here. */
export function everosLocked(r: Role, snap: SettingsSnapshot): 'foreign' | 'env' | null {
  if (!r.everos) return null
  if (snap.everos?.owned === false) return 'foreign'
  return snap.everos?.sections?.[r.everos]?.env_managed ? 'env' : null
}

/* The roles a provider (and optionally one of its models) serves right now.
   A role that follows the chat model counts through the chat role. */
export function rolesUsing(snap: SettingsSnapshot, slug: string, model?: string): Role[] {
  const chat = roleValue(ROLES[0]!, snap)
  return ROLES.filter((r) => {
    const own = roleValue(r, snap)
    const v = own || (r.keys ? chat : null)
    return !!v && v.provider === slug && (model === undefined || v.model === model)
  })
}

const providerName = (snap: SettingsSnapshot, id: string): string => {
  const p = snap.providers.find((x) => x.id === id)
  return p ? p.name : id
}

/* The write a pick makes, by role. The typed id is added to the provider
   first, so the role never names a model the provider does not list. */
const setIn = (raw: Record<string, unknown>, path: string, value: unknown): Record<string, unknown> => {
  const [head, ...rest] = path.split('.')
  const cur = raw[head!]
  return {
    ...raw,
    [head!]: rest.length ? setIn(cur && typeof cur === 'object' ? cur as Record<string, unknown> : {}, rest.join('.'), value) : value,
  }
}

/* The snapshot as it will read once this pick is stored, drawn at once: the
   write is a few milliseconds but the reload behind it is a live read of every
   vendor, and a slot that kept its old model for most of a second read as a
   click that did nothing. */
function withRole(r: Role, snap: SettingsSnapshot, model: string, provider: string): SettingsSnapshot {
  if (r.id === 'chat') return { ...snap, model, curProvider: provider }
  if (r.keys) return { ...snap, raw: setIn(setIn(snap.raw, r.keys[0], model), r.keys[1], provider) }
  if (r.everos && snap.everos) {
    const sections = snap.everos.sections || {}
    return { ...snap, everos: { ...snap.everos, sections: { ...sections, [r.everos]: { ...sections[r.everos], model, provider } } } }
  }
  if (r.media) return { ...snap, raw: setIn(snap.raw, `tools.media.${r.media}`, mediaSelection(r.media, model)) }
  return snap
}

async function setRole(r: Role, model: string, provider: string, typed: boolean, kind?: Kind): Promise<SettingsSnapshot | void> {
  const src = store.source()
  /* The typed id joins the provider's list first, with what the person said it
     is: a name the registry's patterns miss ("our-finetune") would otherwise be
     stored as text and vanish from the very slot it was typed into. */
  if (typed) await src.provider('add_model', { slug: provider, model, ...statedTags(kind ?? 'text') })
  if (r.id === 'chat') {
    if (await src.pickModel(model, provider)) store.set({ needsRestart: true })
    return src.load()
  }
  if (r.keys) { await src.set(r.keys[0], model); return src.set(r.keys[1], provider) }
  if (r.everos) return src.everosSet(r.everos, model, provider)
  if (r.media) {
    await src.set(`tools.media.${r.media}`, mediaSelection(r.media, model))
    return src.set('tools.disabledTools', disabledTools(store.get().snap.raw).filter((x) => x !== r.tool))
  }
  return undefined
}

/* The whole selection the checker wants -- model and quality, nothing else --
   keeping the quality already chosen. An empty model is how the selection is
   cleared: the key takes no null. */
function mediaSelection(kind: string, model: string): { model: string; quality: string } {
  const cur = (dig(store.get().snap.raw, `tools.media.${kind}`) as { quality?: unknown } | undefined) || {}
  return { model, quality: typeof cur.quality === 'string' ? cur.quality : '' }
}

async function clearRole(r: Role): Promise<SettingsSnapshot | void> {
  const src = store.source()
  if (r.keys) { await src.set(r.keys[0], null); return src.set(r.keys[1], null) }
  if (r.everos) return src.everosSet(r.everos, null)
  if (r.media) {
    await src.set(`tools.media.${r.media}`, mediaSelection(r.media, ''))
    const dis = disabledTools(store.get().snap.raw)
    return src.set('tools.disabledTools', r.tool && !dis.includes(r.tool) ? [...dis, r.tool] : dis)
  }
  return undefined
}

/* The pill that shows a role's model and opens the picker under it.

   The picker is the composer's: one component, opened with an offer that says
   what this slot may take (its kind, the providers the role allows, the pair it
   holds now) and what a pick means here (the role's own write, not a
   conversation switch). */
export function RolePill({ role, setup }: { role: Role; setup?: boolean }): JSX.Element {
  /* The picker hangs off this button, so it has to be reachable as an element
     and not only as markup. */
  const pill = useRef<HTMLButtonElement>(null)
  const s = store.get()
  const val = roleValue(role, s.snap)
  const provs = roleProviders(role, s.snap)
  const inherit = !!role.keys
  const chat = roleValue(ROLES[0]!, s.snap)
  /* No provider this role may use is connected: there is nothing to open onto,
     and the way out is the providers page rather than an empty popover. A
     provider that is connected but has nothing of this kind added is NOT this
     case -- the picker lists it, says the column is empty and offers a row to
     type an id into. */
  /* No EverOS to configure: say that, not that the vendors fall short. */
  if (role.everos && s.snap.everos?.available === false && !val) {
    return (
      <span className="settings-mpill settings-dim" title={s.snap.everos.note || undefined}>
        <span className="settings-id">{t('gui.settings.roles.everos_missing')}</span>
      </span>
    )
  }
  if (!provs.length && !val) {
    /* The way out is the providers page, so say so with a button that goes
       there. A sentence that names the page without taking the reader to it
       leaves every slot on a fresh install a dead end -- which is the state a
       fresh install starts in. */
    /* A row names what it is missing only once something is connected; before
       that every row is the same first step. The click still lands on the
       vendor this row needs. */
    const anyOn = s.snap.providers.some((p) => p.on)
    const label = !anyOn
      ? 'gui.settings.roles.no_provider'
      : role.media ? 'gui.settings.roles.connect_openrouter'
        : role.everos ? 'gui.settings.roles.no_vendor_for_role' : 'gui.settings.roles.no_provider'
    /* Drawn in the pill's own box rather than as a button sized to its words:
       on a fresh install most rows are in this state, and three labels of three
       lengths beside the pills of the rows that are served made a ragged
       column. */
    return (
      <span className="settings-mpill settings-dim">
        <button type="button" className="settings-pm" onClick={(e) => {
          /* The wizard has no providers page to go to: its add form sits in the
             card above, so open that on a vendor that can serve this row. */
          if (setup) {
            const off = s.snap.providers.filter((p) => !p.on)
            const fit = role.media
              ? off.find((p) => p.id === MEDIA_PROVIDER)
              : role.everos ? off.find((p) => (s.snap.everos?.supports?.[p.id] || []).includes(role.everos!)) : undefined
            store.set({ provAdd: (fit || off[0])?.id ?? '', err: '' })
            const pane = e.currentTarget.closest('.settings-setup')
            requestAnimationFrame(() => pane?.querySelector('.settings-padd')?.scrollIntoView({ block: 'center', behavior: 'smooth' }))
            return
          }
          /* The tab first: switching a section clears every drawer of the one it
             leaves, `provider` included, so naming the row before the switch
             names it into the state the switch is about to wipe.
             A media role can only run on OpenRouter, so open that row rather than
             leaving the reader to find it among fifty-five. */
          store.setTab('provider')
          if (role.media) store.set({ provider: MEDIA_PROVIDER, provAdd: MEDIA_PROVIDER })
        }}>
          <span className="settings-id">{t(label)}</span>
          <span className="settings-ch">{'›'}</span>
        </button>
      </span>
    )
  }
  /* Shown, not offered. Raven cannot write a root somebody else manages, and it
     cannot edit the shell an EVEROS_* export came from -- a slot that took the
     click and saved anyway would report success for a value that never applies. */
  const locked = everosLocked(role, s.snap)
  if (locked) {
    return (
      <span className="settings-mpill settings-dim" title={t(`gui.settings.roles.locked_${locked}`)}>
        <span className="settings-id">{val ? val.model : t('gui.settings.roles.unset')}</span>
        {val && <span className="settings-pv">{providerName(s.snap, val.provider)}</span>}
      </span>
    )
  }
  const dim = !val
  /* Asked of the server, not remembered here. Clearing `llm` turns long-term
     memory off outright and `embedding` is what every stored vector was written
     under, so the write refuses both -- and this page drew the button anyway
     until the contract came down the wire. */
  const required = s.snap.everos?.required || []
  const clearable = !!val && role.id !== 'chat' && !(role.everos && required.includes(role.everos))
  const cls = ['settings-mpill', dim ? 'settings-dim' : '', clearable ? 'settings-clearable' : ''].filter(Boolean).join(' ')
  const offer: Offer = {
    kind: ROLE_KIND[role.id],
    providers: provs.map((p) => p.id),
    title: roleName(role),
    current: val ?? (inherit && chat ? chat : undefined),
    pick: async (model, provider, typed, kind) => {
      const before = store.get().snap
      const drawn = withRole(role, before, model, provider)
      store.set({ snap: drawn })
      const ok = await store.run(`role:${role.id}`, () => setRole(role, model, provider, typed, kind))
      /* Refused: put back what was there, unless something else has redrawn
         the page since, in which case that is the newer truth. */
      if (!ok && store.get().snap === drawn) store.set({ snap: before })
    },
  }
  return (
    <span className={cls}>
      <button ref={pill} type="button" className="settings-pm" aria-label={t('gui.settings.roles.change', { role: roleName(role) })}
        onClick={() => { if (pill.current) openPicker(pill.current, offer) }}>
        {val ? (
          <><span className="settings-id">{val.model}</span><span className="settings-pv">{providerName(s.snap, val.provider)}</span></>
        ) : (
          <span className="settings-id">{inherit ? t('gui.settings.roles.follows_chat') : t('gui.settings.roles.unset')}</span>
        )}
        <span className="settings-ch">{'⌄'}</span>
      </button>
      {clearable && (
        <button type="button" className="settings-px" aria-label={t('gui.settings.roles.clear', { role: roleName(role) })}
          onClick={() => void store.run(`role:${role.id}`, () => clearRole(role))}>
          {'×'}
        </button>
      )}
    </span>
  )
}

function RoleLabel({ role, extra }: { role: Role; extra?: JSX.Element }): JSX.Element {
  return (
    <div className="settings-k settings-rk">
      <div className="settings-rt">
        {roleName(role)}
        {role.optional && <span className="settings-opttag">{t('gui.settings.roles.optional')}</span>}
      </div>
      <div className="settings-rd">{t(USE[role.id])}</div>
      {extra}
    </div>
  )
}

const modelWindow = (snap: SettingsSnapshot): number | null => {
  const p = snap.providers.find((x) => x.id === snap.curProvider)
  const facts = p && p.labels && p.labels[snap.model]
  const win = facts && (facts as { context_window?: number }).context_window
  return typeof win === 'number' && win > 0 ? win : null
}

const EFFORTS: Array<[string, string]> = [
  ['minimal', 'gui.settings.roles.effort_minimal'], ['low', 'gui.settings.roles.effort_low'],
  ['medium', 'gui.settings.roles.effort_medium'], ['high', 'gui.settings.roles.effort_high'],
]

const effortWord = (v: string): string =>
  t(EFFORTS.find(([id]) => id === v)?.[1] ?? 'gui.settings.roles.effort_low')

/* The three values the parameters drawer edits. Read out here rather than
   inside it, because the closed trigger says what they are. */
function chatParams(raw: Record<string, unknown>): { effort: string; iters: number; pin: number | null } {
  const pinned = dig(raw, 'agents.defaults.contextWindowTokens')
  return {
    effort: (dig(raw, 'agents.defaults.reasoningEffort') as string) || 'low',
    iters: Number(dig(raw, 'agents.defaults.maxToolIterations')) || 40,
    pin: typeof pinned === 'number' && pinned > 0 ? pinned : null,
  }
}

/* The disclosure the chat row carries: a chevron, the word, and -- while it is
   closed -- the values behind it, so none of the three has to be opened to be
   read. The labels are shorter than the drawer's own rows use: the summary has
   to sit on one line beside the role's name, where the full ones would not. */
function ParamsDisc(): JSX.Element {
  const s = store.get()
  const open = s.chatCfg
  const { effort, iters, pin } = chatParams(s.snap.raw)
  const sum: Array<[string, string]> = [
    [t('gui.settings.roles.sum_effort'), effortWord(effort)],
    [t('gui.settings.roles.sum_iters'), t('gui.settings.roles.sum_iters_n', { n: iters })],
    [t('gui.settings.roles.sum_ctx'), pin ? `${pin.toLocaleString()} tok` : t('gui.settings.roles.sum_ctx_auto')],
  ]
  return (
    <button type="button" className="foldcap settings-disc" aria-expanded={open} onClick={() => store.set({ chatCfg: !open })}>
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg>
      <span className="settings-sl">{t('gui.settings.roles.params')}</span>
      {!open && (
        <span className="settings-sum">
          {sum.map(([label, value]) => (
            <span className="settings-sv" key={label}><span className="settings-sn">{label}</span>{value}</span>
          ))}
        </span>
      )}
    </button>
  )
}

/* The chat model's parameters: effort, the tool-iteration cap, the context
   window. Refusals are the page's: outside 1-200, below 1024 tokens. */
function ChatParams(): JSX.Element {
  const s = store.get()
  const raw = s.snap.raw
  const { effort, iters, pin } = chatParams(raw)
  const win = modelWindow(s.snap)
  const bump = (d: number): void => {
    const step = iters >= 100 ? 10 : 1
    const v = iters + d * step
    if (v < ITER_RANGE[0]) { store.refuse(t('gui.settings.roles.min', { n: ITER_RANGE[0] })); return }
    if (v > ITER_RANGE[1]) { store.refuse(t('gui.settings.roles.max', { n: ITER_RANGE[1] })); return }
    void store.write('agents.defaults.maxToolIterations', v)
  }
  const pinChange = (text: string): void => {
    const v = parseInt(text.replace(/[^0-9]/g, ''), 10)
    if (!v || v < CTX_MIN) { store.refuse(t('gui.settings.roles.ctx_min', { n: CTX_MIN })); return }
    void store.write('agents.defaults.contextWindowTokens', v)
  }
  return (
    <div className="settings-cfg">
      <Row label={t('gui.settings.roles.effort')}>
        <Seg
          opts={EFFORTS.map(([v, key]): [string, string] => [v, t(key)])}
          value={effort}
          onPick={(v) => void store.write('agents.defaults.reasoningEffort', v)}
        />
      </Row>
      <Row label={t('gui.settings.roles.iterations')}>
        <Stepper value={iters} unit={t('gui.settings.roles.times')} onBump={bump} />
      </Row>
      <Row label={t('gui.settings.roles.ctx')}>
        <span className="settings-taglist">
          {pin ? (
            <>
              <input className="settings-tbox settings-ctxbox" defaultValue={pin} inputMode="numeric"
                aria-label={t('gui.settings.roles.ctx_fixed')}
                onBlur={(e) => { if (Number(e.currentTarget.value) !== pin) pinChange(e.currentTarget.value) }}
                onKeyDown={(e) => { if (e.key === 'Enter') pinChange(e.currentTarget.value) }} />
              <span className="settings-fl2">tok</span>
            </>
          ) : (
            <Rov>{win ? `${win.toLocaleString()} tok` : t('gui.settings.roles.ctx_unknown')}</Rov>
          )}
          <Seg
            opts={[['auto', t('gui.settings.roles.ctx_auto')], ['pin', t('gui.settings.roles.ctx_pin')]]}
            value={pin ? 'pin' : 'auto'}
            onPick={(v) => void store.write('agents.defaults.contextWindowTokens', v === 'auto' ? null : (win || 128000))}
          />
        </span>
      </Row>
      {pin && win && pin > win && (
        <div className="settings-cfnote">{t('gui.settings.roles.ctx_over', { n: win.toLocaleString() })}</div>
      )}
    </div>
  )
}

/* A few role rows with no card around them, for a page that needs only those
   (the wizard's data-sync step asks for its embedding model in place).
   `needed` drops the "optional" tag: the page drawing the row requires it. */
export function RoleRows({ ids, setup, needed }: { ids: RoleId[]; setup?: boolean; needed?: boolean }): JSX.Element {
  return (
    <div className="settings-rows">
      {ROLES.filter((r) => ids.includes(r.id)).map((r) => (
        <Row key={r.id} k={<RoleLabel role={needed ? { ...r, optional: false } : r} />}>
          <RolePill role={r} setup={setup} />
        </Row>
      ))}
    </div>
  )
}

export function Roles({ setup }: { setup?: boolean }): JSX.Element {
  const s = store.get()
  return (
    <Card title={t('gui.settings.roles.title_card')}>
      {ROLES.map((r) => (
        <div key={r.id}>
          <Row open={r.id === 'chat' && s.chatCfg} k={(
            <RoleLabel role={r} extra={r.id === 'chat' ? <ParamsDisc /> : undefined} />
          )}>
            <RolePill role={r} setup={setup} />
          </Row>
          {r.id === 'chat' && s.chatCfg && <ChatParams />}
        </div>
      ))}
    </Card>
  )
}
