/* The model roles: which model each job runs on, and the picker that changes
   it. Nine rows; each knows where its pair is written and which providers
   may serve it. The media tools' rows on the Tools page draw the same pill. */
import { useRef } from 'react'

import { t } from '../../../i18n/t'
import {
  MEDIA_PROVIDER, ROLE_KIND, ROLES, disabledTools, everosLocked, followsChat,
  mediaSelection, providerName, roleProviders, roleValue, rolesUsing, withRole,
} from '../../../lib/modelRoles'
import { openPicker } from '../../model/source'
import { statedTags } from '../../model/types'
import { Card, Row, Rov, Seg, Stepper } from '../Fields'
import { dig } from '../source'
import * as store from '../store'

import type { Role, RoleId, RoleValue } from '../../../lib/modelRoles'
import type { Kind, Offer } from '../../model/types'
import type { SettingsSnapshot } from '../types'
import type { JSX } from 'react'

export {
  ROLE_KIND, ROLES, disabledTools, everosLocked, followsChat, roleProviders, roleValue, rolesUsing,
}
export type { Role, RoleId, RoleValue }

/* Literal keys, for the i18n gate. */
const NAME: Record<RoleId, string> = {
  chat: 'gui.settings.roles.chat', title: 'gui.settings.roles.title',
  memllm: 'gui.settings.roles.memllm', embedding: 'gui.settings.roles.embedding',
  rerank: 'gui.settings.roles.rerank', multimodal: 'gui.settings.roles.multimodal', image: 'gui.settings.roles.image',
  speech: 'gui.settings.roles.speech', video: 'gui.settings.roles.video',
}
const USE: Record<RoleId, string> = {
  chat: 'gui.settings.roles.chat_use', title: 'gui.settings.roles.title_use',
  memllm: 'gui.settings.roles.memllm_use', embedding: 'gui.settings.roles.embedding_use',
  rerank: 'gui.settings.roles.rerank_use', multimodal: 'gui.settings.roles.multimodal_use', image: 'gui.settings.roles.image_use',
  speech: 'gui.settings.roles.speech_use', video: 'gui.settings.roles.video_use',
}
export const roleName = (r: Role): string => t(NAME[r.id])

const CTX_MIN = 1024
const ITER_RANGE: [number, number] = [1, 200]
const ITER_DEFAULT = 40

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
    await src.set(`tools.media.${r.media}`, mediaSelection(store.get().snap.raw, r.media, model, provider))
    return src.set('tools.disabledTools', disabledTools(store.get().snap.raw).filter((x) => x !== r.tool))
  }
  return undefined
}

async function clearRole(r: Role): Promise<SettingsSnapshot | void> {
  const src = store.source()
  if (r.keys) { await src.set(r.keys[0], null); return src.set(r.keys[1], null) }
  if (r.everos) return src.everosSet(r.everos, null)
  if (r.media) {
    await src.set(`tools.media.${r.media}`, mediaSelection(store.get().snap.raw, r.media, ''))
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
  const inherit = followsChat(role, s.snap)
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
             A media role's default is OpenRouter, the one provider every media
             tool runs on, so open that row rather than leaving the reader to
             find it among fifty-six. */
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

/* The first rung is the key unset: the provider's own default, which is what
   an install that never touched the setting runs at. Drawn as a rung of its
   own rather than read as Low, which is what this did before and what made
   the page claim a setting the file does not hold. */
const EFFORTS: Array<[string, string]> = [
  ['', 'gui.settings.roles.effort_default'],
  ['minimal', 'gui.settings.roles.effort_minimal'], ['low', 'gui.settings.roles.effort_low'],
  ['medium', 'gui.settings.roles.effort_medium'], ['high', 'gui.settings.roles.effort_high'],
]

const effortWord = (v: string): string =>
  t(EFFORTS.find(([id]) => id === v)?.[1] ?? 'gui.settings.roles.effort_default')

/* The cap as the loader reads it, not as the number control wants it: an
   explicit null or 0 is the file saying "no cap", a positive int is the cap,
   and an absent key keeps the built-in 40. The two spellings are tried in the
   loader's order, camelCase first, because the value shown has to be the one
   the turn runs under. */
interface IterCap { unlimited: boolean; value: number }
function iterCap(raw: Record<string, unknown>): IterCap {
  for (const key of ['agents.defaults.maxToolIterations', 'agents.defaults.max_tool_iterations']) {
    const v = dig(raw, key)
    if (v === undefined || typeof v === 'boolean') continue
    if (v === null) return { unlimited: true, value: ITER_DEFAULT }
    if (typeof v === 'number' && Number.isInteger(v)) {
      if (v === 0) return { unlimited: true, value: ITER_DEFAULT }
      return { unlimited: false, value: v > 0 ? v : ITER_DEFAULT }
    }
  }
  return { unlimited: false, value: ITER_DEFAULT }
}

/* The three values the parameters drawer edits. Read out here rather than
   inside it, because the closed trigger says what they are. */
function chatParams(raw: Record<string, unknown>): { effort: string; iters: IterCap; pin: number | null } {
  const pinned = dig(raw, 'agents.defaults.contextWindowTokens')
  return {
    effort: (dig(raw, 'agents.defaults.reasoningEffort') as string) || '',
    iters: iterCap(raw),
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
    [t('gui.settings.roles.sum_iters'), iters.unlimited
      ? t('gui.settings.roles.iterations_unbounded')
      : t('gui.settings.roles.sum_iters_n', { n: iters.value })],
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
    const step = iters.value >= 100 ? 10 : 1
    const v = iters.value + d * step
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
          /* The default rung clears the key: null hands the choice back to the provider. */
          onPick={(v) => void store.write('agents.defaults.reasoningEffort', v || null)}
        />
      </Row>
      <Row label={t('gui.settings.roles.iterations')}>
        {iters.unlimited
          ? <Rov>{t('gui.settings.roles.iterations_unbounded')}</Rov>
          : <Stepper value={iters.value} unit={t('gui.settings.roles.times')} onBump={bump} />}
      </Row>
      {iters.unlimited && (
        <div className="settings-addhint">{t('gui.settings.roles.iterations_unbounded_note')}</div>
      )}
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
  /* Another memory backend reads none of EverOS's extraction models, so an
     EverOS slot is drawn only when the roles RPC still describes it in
     `sections` (embedding, when the local plugin is installed: a knowledge base
     embeds with that pin whatever the backend is). Keyed on the RPC's reason
     and sections, not on the cloud card, so a third backend hides them too and
     the server alone says which rows survive. */
  const everos = s.snap.everos
  const other = everos?.reason === 'other_backend'
  return (
    <Card title={t('gui.settings.roles.title_card')}>
      {ROLES.filter((r) => !(other && r.everos && !(r.everos in (everos?.sections ?? {})))).map((r) => (
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
