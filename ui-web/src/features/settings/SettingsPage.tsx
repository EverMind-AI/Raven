import { Fragment, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { shell, t } from '../../shell/bridge'
import { KeyInput } from '../../shell/key-input'
import * as lookStore from '../../shell/look'
import * as notifications from '../../shell/notifications'
import { open as openUrl } from '../../shell/open-url'
import { isMac, modKey } from '../../shell/platform'
import { ModelTagDefs, ModelTags, TagGlyph } from '../../shell/model-tags'
import { ModelIcon, ProviderIcon, ProviderLink, ProviderStatus } from '../../shell/provider-mark'
import { hint as reachHint, text as reachText } from '../../shell/reach'
import { show as toast } from '../../shell/toast'
import { open as openConn } from '../connections/store'
import { count as sessionCount, deleteAll as deleteAllSessions } from '../rail/store'
import * as store from './store'
import { ImageModelPicker } from './ImageModelPicker'

import type { SettingsState } from './store'
import type { EverosSection, ProviderRow, ToolGroup, ToolRow } from './types'
import type { JSX, ReactNode, RefObject } from 'react'

/* The dialog's contents, transcribed from the legacy drawSettings pages:
   same class names, same DOM shape, ui-web/src/styles/page.css untouched. The
   dialog frame (#setVeil / #setModal, open and close) stays legacy chrome. */

/* Settings is grouped, not one flat strip: the groups answer "what am I
   changing" -- myself, the agent, or the machine it runs on. */
const SET_GROUPS: Array<{ key: string; pages: Array<[string, string]> }> = [
  {
    key: 'gui.set.grp.me',
    pages: [
      ['usage', 'gui.set.pg.usage'],
      ['look', 'gui.set.pg.look'],
      ['notify', 'gui.set.pg.notify'],
      ['keys', 'gui.set.pg.keys'],
      ['about', 'gui.set.pg.about'],
    ],
  },
  {
    key: 'gui.set.grp.agent',
    pages: [
      ['model', 'gui.set.pg.model'],
      ['perm', 'gui.set.pg.perm'],
      ['toolset', 'gui.set.pg.toolset'],
      ['memory', 'gui.set.pg.memory'],
      ['proact', 'gui.set.pg.proact'],
    ],
  },
  {
    key: 'gui.set.grp.env',
    pages: [
      ['exec', 'gui.set.pg.exec'],
      ['channel', 'gui.set.pg.channel'],
      ['data', 'gui.set.pg.data'],
    ],
  },
]
const SET_TITLE: Record<string, string> = {}
SET_GROUPS.forEach((g) => g.pages.forEach(([id, key]) => (SET_TITLE[id] = key)))

/* One glyph per section: with 14 of them in a 208px rail, a shape is what the
   eye returns to, and the words are what it reads once it is there. */
const SET_ICO: Record<string, string> = {
  account: '<circle cx="12" cy="8.5" r="3.6"/><path d="M5 20c1.2-3.5 3.8-5.2 7-5.2s5.8 1.7 7 5.2"/>',
  usage: '<path d="M4 19h16"/><path d="M7 19v-6M12 19V6M17 19v-9"/>',
  look: '<circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 0 0 16Z" fill="currentColor" stroke="none"/>',
  notify: '<path d="M12 4a5 5 0 0 0-5 5v4l-1.5 3h13L17 13V9a5 5 0 0 0-5-5Z"/><path d="M10 20h4"/>',
  keys: '<rect x="3" y="6.5" width="18" height="11" rx="2.2"/><path d="M7 10h.01M11 10h.01M15 10h.01M8 14h8"/>',
  about: '<circle cx="12" cy="12" r="8"/><path d="M12 11v5M12 8h.01"/>',
  model: '<path d="M12 3.5 20 8v8l-8 4.5L4 16V8l8-4.5Z"/><path d="M12 12v8.5M12 12 4 8M12 12l8-4"/>',
  perm: '<path d="M12 3.5 19 6v5.5c0 4-2.9 7.4-7 9-4.1-1.6-7-5-7-9V6l7-2.5Z"/>',
  memory: '<rect x="4" y="4.5" width="16" height="15" rx="2.4"/><path d="M8 9h8M8 12.5h8M8 16h5"/>',
  proact: '<path d="M13 3 5.5 13.5H11l-1 7.5 8-11H12l1-7Z"/>',
  exec: '<rect x="3.5" y="5" width="17" height="14" rx="2.4"/><path d="m7.5 10 2.5 2-2.5 2M13 14h4"/>',
  toolset:
    '<path d="M14.5 4.5a4.2 4.2 0 0 0 5.5 5.6L14 16.2l-4-4 4.5-7.7Z"/><path d="m9 13-4.5 4.5a1.8 1.8 0 0 0 2.5 2.5L11.5 16"/>',
  tools: '<circle cx="8" cy="12" r="3.5"/><path d="M11.5 12H20M16.5 12v3M20 12v2.5"/>',
  channel: '<path d="M4 7.5h16v9H4z"/><path d="m4 8 8 5 8-5"/>',
  data: '<ellipse cx="12" cy="6.5" rx="7" ry="2.8"/><path d="M5 6.5v11c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8v-11"/><path d="M5 12c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8"/>',
}

/* V() and its null-keeping sibling, over the snapshot's raw config. */
function V(raw: Record<string, unknown>, path: string, fallback: unknown): unknown {
  const v = String(path)
    .split('.')
    .reduce<unknown>((o, k) => (o == null ? o : (o as Record<string, unknown>)[k]), raw)
  return v == null || v === '' ? fallback : v
}
function Vnull(raw: Record<string, unknown>, path: string, fallback: unknown): unknown {
  let node: unknown = raw
  for (const k of String(path).split('.')) {
    if (node == null || typeof node !== 'object' || !(k in (node as Record<string, unknown>))) return fallback
    node = (node as Record<string, unknown>)[k]
  }
  return node
}

const onoff = (v: boolean): string => t(v ? 'gui.set.on' : 'gui.set.off')
const shortModel = (m: string): string => String(m || '').split('/').pop() ?? ''

/* The vendor a model id is filed under, within the provider serving it.
 *
 * A stored id leads with the provider (`siliconflow/BAAI/bge-m3`), and what is
 * left is how that provider files the model -- `BAAI` here, `anthropic` for
 * OpenRouter's Claude rows. Providers that publish flat ids (`minimax-global/
 * MiniMax-M3`) have no vendor to report, and get ''. */
const modelGroup = (providerId: string, model: string): string => {
  const flat = (name: string): string => name.toLowerCase().replace(/[^a-z0-9]/g, '')
  const parts = String(model || '').split('/')
  if (parts.length > 1 && flat(parts[0] as string) === flat(providerId)) parts.shift()
  return parts.length > 1 ? (parts[0] as string) : ''
}

/* Vendors whose own spelling of their name is not what capitalising the id
 * produces. Only those: `qwen` -> `Qwen` and `tencent` -> `Tencent` need no
 * entry, and a table listing every vendor would be a table to keep current.
 *
 * A segment that already carries a capital is left exactly as published --
 * `BAAI`, `MiniMaxAI`, `Kwai-Kolors`, `TheDrummer`. Whoever wrote it that way
 * meant it, and no rule here can improve on that. */
const VENDOR_NAMES: Record<string, string> = {
  '01-ai': '01.AI',
  'abacusai': 'Abacus AI',
  'ai21': 'AI21',
  'aion-labs': 'AION Labs',
  'allenai': 'AllenAI',
  'anthracite-org': 'Anthracite',
  'arcee-ai': 'Arcee AI',
  'baai': 'BAAI',
  'bytedance-seed': 'ByteDance Seed',
  'bytedance': 'ByteDance',
  'canopylabs': 'Canopy Labs',
  'deepseek-ai': 'DeepSeek',
  'deepseek': 'DeepSeek',
  'elevenlabs': 'ElevenLabs',
  'ibm-granite': 'IBM Granite',
  'ideogramai': 'Ideogram AI',
  'inclusionai': 'inclusionAI',
  'internlm': 'InternLM',
  'meta-llama': 'Meta Llama',
  'minimaxai': 'MiniMax',
  'minimax': 'MiniMax',
  'mistralai': 'Mistral AI',
  'moonshotai': 'Moonshot AI',
  'nanogpt': 'NanoGPT',
  'nousresearch': 'Nous Research',
  'nvidia': 'NVIDIA',
  'openai': 'OpenAI',
  'opengvlab': 'OpenGVLab',
  'openrouter': 'OpenRouter',
  'rekaai': 'Reka AI',
  'shisa-ai': 'Shisa AI',
  'stepfun-ai': 'StepFun',
  'stepfun': 'StepFun',
  'thedrummer': 'TheDrummer',
  'thinkingmachines': 'Thinking Machines',
  'thudm': 'THUDM',
  'tii': 'TII',
  'voyageai': 'Voyage AI',
  'x-ai': 'xAI',
  'xai': 'xAI',
  'z-ai': 'Z.ai',
  'zai-org': 'Z.ai',
  'zai': 'Z.ai',
}

/* A vendor segment as its vendor writes it. Ids arrive lowercased from most
 * gateways -- `openai/gpt-6`, `x-ai/grok-4` -- and a heading that says
 * "openai" reads like a path fragment rather than a name. */
const vendorLabel = (name: string): string => {
  if (/[A-Z]/.test(name)) return name
  const known = VENDOR_NAMES[name]
  if (known) return known
  return name
    .split(/[-_]/)
    .map((word) => (word ? word[0]!.toUpperCase() + word.slice(1) : word))
    .join(' ')
}

/* What a row has to say once its heading has said the rest.
 *
 * A row under a "BAAI" heading called "BAAI/bge-m3" says BAAI twice, and one
 * under a provider's own panel repeats the provider on every line. So the
 * provider comes off, the group comes off, and what is left is the part that
 * tells the rows apart. A published name is trusted except for the same
 * doubling -- upstream writes both "BAAI/bge-m3" and "BAAI: BGE M3". */
const rowName = (providerId: string, group: string, id: string, label?: string): string => {
  const flat = (name: string): string => name.toLowerCase().replace(/[^a-z0-9]/g, '')
  if (label) {
    for (const sep of ['/', ': ', ':', ' - ']) {
      const head = group + sep
      if (group && label.toLowerCase().startsWith(head.toLowerCase())) return label.slice(head.length).trim()
    }
    return label
  }
  const parts = String(id || '').split('/')
  if (parts.length > 1 && flat(parts[0] as string) === flat(providerId)) parts.shift()
  if (parts.length > 1 && group && flat(parts[0] as string) === flat(group)) parts.shift()
  return parts.join('/')
}

/* The list in the order it was given, cut into vendor groups -- or not cut at
 * all. Grouping is only worth a header when there is more than one: a single
 * heading over the whole list names something the panel already says, and rows
 * with no vendor at all lead the list rather than sitting under one. */
function groupModels<T>(providerId: string, rows: T[], idOf: (row: T) => string): Array<[string, T[]]> {
  const groups: Array<[string, T[]]> = []
  for (const row of rows) {
    const name = modelGroup(providerId, idOf(row))
    const bucket = groups.find(([key]) => key === name)
    if (bucket) bucket[1].push(row)
    else groups.push([name, [row]])
  }
  const named = groups.filter(([key]) => key).length
  return named > 1 ? groups : [['', rows]]
}
const kindLabel = (kind?: string): string => t('gui.model.kind.' + (kind || 'key'), undefined, t('gui.model.kind.key'))

/* The one-line description the pane header carries: how the provider is
   reached, what it lends the picker, and whether it is waiting on a login. */
const summary = (pv: ProviderRow): string[] => {
  const bits = [pv.on ? t('gui.model.state.connected') : kindLabel(pv.kind)]
  /* The list this pane manages, so the count and the rows under it agree. */
  const n = (pv.configured ?? []).length
  if (n) bits.push(t('gui.model.count', { n }))
  if (!pv.on && pv.kind === 'oauth') bits.push(t('gui.model.needs_login'))
  return bits
}
const loginCmd = (slug: string): string => `raven provider login ${String(slug).replace(/_/g, '-')}`

/* The in-row refusal (the legacy nlSay): a tagged control never renders the
   new value, and the refusal is spoken in the row, not in a toast. */
function useNl(): [string, () => void] {
  const [msg, setMsg] = useState('')
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(timer.current), [])
  const say = (): void => {
    setMsg(t('gui.set.not_live'))
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setMsg(''), 3600)
  }
  return [msg, say]
}

const Tick = (): JSX.Element => (
  <svg className="tick" viewBox="0 0 24 24" aria-hidden="true">
    <path d="m5 12.5 4.5 4.5L19 7" />
  </svg>
)

const UpdateIcon = (): JSX.Element => (
  <svg className="update-icon" viewBox="0 0 24 24" aria-hidden="true">
    <path d="M20 11a8 8 0 0 0-13.7-5.7L4 7.6" />
    <path d="M4 4v3.6h3.6" />
    <path d="M4 13a8 8 0 0 0 13.7 5.7L20 16.4" />
    <path d="M20 20v-3.6h-3.6" />
  </svg>
)

const CopyIcon = (): JSX.Element => (
  <svg className="copy-icon" viewBox="0 0 24 24" aria-hidden="true">
    <rect x="9" y="9" width="11.5" height="11.5" rx="2.6" />
    <path d="M15.5 5.8A2.8 2.8 0 0 0 12.7 3H6.3A3.3 3.3 0 0 0 3 6.3v6.4a2.8 2.8 0 0 0 2.8 2.8" />
  </svg>
)

/* The API host field: the input, plus a copy sitting in its right-hand gutter
   the way the key field carries its eye.
 *
 * An address is read back far more often than it is retyped -- into a curl, a
 * second machine, an answer to "what is it pointed at" -- and dragging a
 * selection across an input to get it is the fiddliest way to do that. The
 * button copies what the field currently holds, not the value it was given, so
 * an edited-but-unsaved address copies as edited.
 *
 * Hidden until the field is pointed at or focused: at rest the pane should
 * read as values, not as a row of controls. */
function HostInput({
  inputRef,
  value,
  placeholder,
}: {
  inputRef: RefObject<HTMLInputElement | null>
  value: string
  placeholder: string
}): JSX.Element {
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(timer.current), [])
  const label = t(copied ? 'gui.model.copied' : 'gui.model.copy')
  return (
    <span className="hostfield">
      <input
        ref={inputRef}
        type="text"
        defaultValue={value}
        placeholder={placeholder}
        autoComplete="off"
        spellCheck={false}
      />
      <button
        type="button"
        className="hcopy"
        data-tip={label}
        aria-label={label}
        title={label}
        tabIndex={-1}
        onClick={() => {
          const text = inputRef.current?.value.trim() ?? ''
          if (!text) return
          if (navigator.clipboard) void navigator.clipboard.writeText(text)
          setCopied(true)
          clearTimeout(timer.current)
          timer.current = setTimeout(() => setCopied(false), 1200)
        }}
      >
        <CopyIcon />
      </button>
    </span>
  )
}

function Scard({ title, desc, children }: { title?: string; desc?: string; children?: ReactNode }): JSX.Element {
  return (
    <div className="scard">
      {(title || desc) && (
        <div className="ch">
          {title && <div className="t">{title}</div>}
          {desc && <div className="d">{desc}</div>}
        </div>
      )}
      {children}
    </div>
  )
}

function Crow({ label, hint, nl, children }: { label: string; hint?: string; nl?: string; children: ReactNode }): JSX.Element {
  return (
    <div className="crow">
      <div className="k">{label}</div>
      {hint && <div className="h">{hint}</div>}
      <div className="c">{children}</div>
      {nl && <div className="nlmsg">{nl}</div>}
    </div>
  )
}

/* Uncontrolled, committed on the native change event -- the discipline the
   legacy textField kept, so focus and IME survive typing. */
function TextField({
  val,
  ph,
  width,
  numeric,
  commit,
}: {
  val: string
  ph?: string
  width: string
  numeric?: boolean
  commit: (v: string, el: HTMLInputElement) => void
}): JSX.Element {
  const ref = useRef<HTMLInputElement>(null)
  const fn = useRef(commit)
  fn.current = commit
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const h = (): void => fn.current(el.value, el)
    el.addEventListener('change', h)
    return () => el.removeEventListener('change', h)
  }, [])
  return (
    <input
      ref={ref}
      type="text"
      defaultValue={val || ''}
      placeholder={ph || ''}
      style={{ width }}
      inputMode={numeric ? 'numeric' : undefined}
    />
  )
}

function SwiRow({
  label,
  hint,
  k,
  on,
  confirm,
}: {
  label: string
  hint?: string
  k: string
  on: boolean
  confirm?: (commit: () => void) => void
}): JSX.Element {
  const [nl, say] = useNl()
  const commit = (): void => {
    void store.write(k, !on).then((r) => {
      if (r === 'notlive') say()
    })
  }
  return (
    <Crow label={label} hint={hint} nl={nl}>
      <button
        className="swi"
        role="switch"
        aria-checked={on}
        aria-label={label}
        onClick={() => {
          if (!on && confirm) confirm(commit)
          else commit()
        }}
      />
    </Crow>
  )
}

function TextRow({
  label,
  hint,
  k,
  val,
  ph,
  norm,
}: {
  label: string
  hint?: string
  k: string
  val: string
  ph?: string
  norm?: (v: string) => unknown
}): JSX.Element {
  const [nl, say] = useNl()
  const commit = (v: string, el: HTMLInputElement): void => {
    const out = norm ? norm(v) : v
    if (out === undefined) {
      el.value = val || ''
      return
    }
    void store.write(k, out).then((r) => {
      if (r === 'notlive') {
        el.value = val || ''
        say()
      }
    })
  }
  return (
    <Crow label={label} hint={hint} nl={nl}>
      <TextField val={val} ph={ph} width="230px" commit={commit} />
    </Crow>
  )
}

function NumRow({ label, hint, k, val, min, max }: { label: string; hint?: string; k: string; val: number; min: number; max: number }): JSX.Element {
  const [nl, say] = useNl()
  const commit = (v: string, el: HTMLInputElement): void => {
    const n = Number(v)
    if (!Number.isInteger(n) || n < min || n > max) {
      el.value = String(val)
      return
    }
    void store.write(k, n).then((r) => {
      if (r === 'notlive') {
        el.value = String(val)
        say()
      }
    })
  }
  return (
    <Crow label={label} hint={hint} nl={nl}>
      <TextField val={String(val)} width="90px" numeric commit={commit} />
    </Crow>
  )
}

/* opts: [value, name, why]. Options that need no sentence render as a compact
   segment: a card grid with nothing to say per card is just empty space. */
function Pick({ opts, val, onPick }: { opts: Array<[string, string, string?]>; val: string; onPick: (v: string) => void }): JSX.Element {
  if (opts.every((o) => !o[2])) {
    return (
      <div className="sgm">
        {opts.map(([v, name]) => (
          <button key={v} className="pk" aria-pressed={v === val} onClick={() => onPick(v)}>
            {name}
          </button>
        ))}
      </div>
    )
  }
  return (
    <div className={`spick n${opts.length}`}>
      {opts.map(([v, name, why]) => (
        <button key={v} className="pk" aria-pressed={v === val} onClick={() => onPick(v)}>
          <div className="n">
            {name}
            <Tick />
          </div>
          {why && <div className="w">{why}</div>}
        </button>
      ))}
    </div>
  )
}

/* The write-through chooser refuses card-wide, so the refusal is a toast --
   the same fallback the legacy nlSay(null) took. */
function WPick({ k, opts, val }: { k: string; opts: Array<[string, string, string?]>; val: string }): JSX.Element {
  return (
    <Pick
      opts={opts}
      val={val}
      onPick={(v) => {
        void store.write(k, v).then((r) => {
          if (r === 'notlive') toast(t('gui.set.not_live'))
        })
      }}
    />
  )
}

function StatTiles({ rows }: { rows: Array<[string | null, string, string?]> }): JSX.Element {
  return (
    <div className="stats">
      {rows.map(([v, k, note], i) => (
        <div className="stat" key={i}>
          <div className={'v' + (v == null ? ' none' : '')}>{v == null ? t('gui.set.nodata') : String(v)}</div>
          <div className="k">{k}</div>
          {note && <div className="note">{note}</div>}
        </div>
      ))}
    </div>
  )
}

/* rows: [label, value, kind]. kind '' | 'unset' | 'ok' */
function KvList({ rows, prose = false }: { rows: Array<[string, string, string?]>; prose?: boolean }): JSX.Element {
  return (
    <div className={'skv' + (prose ? ' text-values' : '')}>
      {rows.map(([k, v, kind], i) => (
        <div className="r" key={i}>
          <span className="k">{k}</span>
          <span className={'v' + (kind ? ' ' + kind : '')}>{v}</span>
        </div>
      ))}
    </div>
  )
}

/* Copy says so on itself: a toast from this layer lands in the console. */
function CpBtn({ label, text }: { label: string; text: string }): JSX.Element {
  const [said, setSaid] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(timer.current), [])
  return (
    <button
      className="mini ghost"
      onClick={() => {
        if (navigator.clipboard) void navigator.clipboard.writeText(String(text))
        setSaid(true)
        clearTimeout(timer.current)
        timer.current = setTimeout(() => setSaid(false), 1600)
      }}
    >
      {said ? t('gui.set.copied') : label}
    </button>
  )
}

function Srmk({ configPath }: { configPath: string }): JSX.Element {
  return (
    <div className="srmk">
      <span>{t('gui.set.prm.file_only')}</span>
      <code>{configPath}</code>
      <CpBtn label={t('gui.set.abt.copy_path')} text={configPath} />
    </div>
  )
}

/* ---- appearance ------------------------------------------------------ */

function Shot({ kind }: { kind: string }): JSX.Element {
  return (
    <div className={'shot ' + kind}>
      <div className="r1" />
      <div className="r2">
        <div className="bar hd w45" />
        <div className="bar w70" />
        <div className="bar w45" />
      </div>
    </div>
  )
}

function LookPage(): JSX.Element {
  const look = {
    ...lookStore.get(),
    lang: document.documentElement.lang.toLowerCase().startsWith('zh') ? 'zh' : 'en',
  }
  const setLook = (patch: Partial<lookStore.LookState>): void => {
    lookStore.set(patch)
    store.redraw()
  }
  return (
    <>
      {/* Language first: it is the one switch that relabels every other row
          here, in the TUI, and in what the agent writes back. */}
      <Scard title={t('gui.set.language')}>
        <Pick
          opts={[
            ['zh', t('gui.set.language_zh')],
            ['en', t('gui.set.language_en')],
          ]}
          val={look.lang}
          onPick={(v) => store.source().setLang(v)}
        />
      </Scard>
      <Scard title={t('gui.set.theme')}>
        <div className="spick n3 thpick">
          {(
            [
              ['system', t('gui.set.theme_system')],
              ['light', t('gui.set.theme_light')],
              ['dark', t('gui.set.theme_dark')],
            ] as Array<[string, string]>
          ).map(([v, name]) => (
            <button key={v} className="pk" aria-pressed={v === look.theme} onClick={() => setLook({ theme: v })}>
              {v === 'system' ? (
                <div className="sysgrid">
                  <Shot kind="lt" />
                  <Shot kind="dk" />
                </div>
              ) : (
                <Shot kind={v === 'light' ? 'lt' : 'dk'} />
              )}
              <div className="lb">
                {name}
                <Tick />
              </div>
            </button>
          ))}
        </div>
      </Scard>
      <Scard title={t('gui.set.codefont')}>
        <div className="spick n3 fpick">
          {(
            [
              ['system', t('gui.set.codefont_system'), 'ui-monospace, monospace'],
              ['jet', 'JetBrains Mono', '"JetBrains Mono", ui-monospace, monospace'],
              ['sf', 'SF Mono', '"SF Mono", "SFMono-Regular", ui-monospace, monospace'],
            ] as Array<[string, string, string]>
          ).map(([v, name, stack]) => (
            <button key={v} className="pk" aria-pressed={v === look.codeFont} onClick={() => setLook({ codeFont: v })}>
              <div className="n">
                {name}
                <Tick />
              </div>
              <div className="smp" style={{ fontFamily: stack }}>
                {'const ok = 0 != O;'}
              </div>
            </button>
          ))}
        </div>
      </Scard>
      <Scard>
        <Crow label={t('gui.set.motion')} hint={t('gui.set.motion_w')}>
          <button
            className="swi"
            role="switch"
            aria-checked={look.motion === 'off'}
            aria-label={t('gui.set.motion')}
            onClick={() => setLook({ motion: look.motion === 'off' ? 'on' : 'off' })}
          />
        </Crow>
      </Scard>
    </>
  )
}

/* ---- notifications --------------------------------------------------- */

function NotifyPage(): JSX.Element {
  const on = notifications.enabled()
  const canNtf = 'Notification' in window
  const flip = async (v: boolean): Promise<void> => {
    if (v && canNtf && Notification.permission !== 'granted') {
      const r = await Notification.requestPermission()
      if (r !== 'granted') {
        notifications.setEnabled(false)
        store.redraw()
        toast(t('gui.set.ntf.denied'))
        return
      }
    }
    notifications.setEnabled(v && canNtf)
    store.redraw()
    if (v && !canNtf) toast(t('gui.set.ntf.denied'))
  }
  return (
    <Scard title={t('gui.set.ntf.all')}>
      <Crow label={t('gui.set.ntf.done')}>
        <button className="swi" role="switch" aria-checked={on} aria-label={t('gui.set.ntf.all')} onClick={() => void flip(!on)} />
      </Crow>
      {on && (
        <Crow label={t('gui.set.ntf.test')}>
          <button className="mini ghost" onClick={() => notifications.show(t('gui.set.ntf.test_body'), '', { force: true })}>
            {t('gui.set.ntf.test')}
          </button>
        </Crow>
      )}
    </Scard>
  )
}

/* ---- usage ----------------------------------------------------------- */

const fmtTok = (n: number): string => (n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n))

function usageCost(value: number | null | undefined, missing: number): string {
  const amount = value == null ? t('gui.set.usg.unknown')
    : value === 0 ? '$0'
    : value < 0.0001 ? '<$0.0001' : '$' + value.toFixed(4)
  return missing ? amount + ' · ' + t('gui.set.usg.cost_missing', { n: missing }) : amount
}

function cacheUsage(value: number | null | undefined, missing: number): string {
  const count = value == null ? t('gui.set.usg.unknown') : fmtTok(value)
  return missing ? count + ' · ' + t('gui.set.usg.cache_missing', { n: missing }) : count
}

function UsagePage({ s }: { s: SettingsState }): JSX.Element {
  /* Only the tick lives here, and it asks the dialog whether it is still up.
     Mounting is not that question: this root mounts at boot with `usage` as
     the starting tab, so a poll begun on mount would read the counters off a
     server whose Settings nobody opened -- and closing the dialog unmounts
     nothing, so a poll begun on mount would never stop either. The reads that
     answer "show me the numbers now" belong to open and to the tab switch,
     which is where the store does them. */
  useEffect(() => {
    const timer = setInterval(() => {
      if (shell().setIsOpen?.()) void store.usageLoad()
    }, 15000)
    return () => clearInterval(timer)
  }, [])
  const u = s.usage
  if (!u) {
    return (
      <Scard>
        <div className="empty-note">{t(u === null ? 'gui.set.nodata' : 'gui.set.usg.loading')}</div>
      </Scard>
    )
  }
  return (
    <>
      <select aria-label="Task usage" value={s.usageSession} onChange={event => store.usageSelect(event.target.value)}>
        <option value="">{t('gui.set.usg.all_tasks')}</option>
        {(u.sessions || []).map(key => <option key={key} value={key}>{u.session_titles?.[key] || key}</option>)}
      </select>
      <Scard title={`${t('gui.set.usg.llm')} · ${t('gui.set.usg.window', { d: u.days })}`}>
        <StatTiles
          rows={[
            [String(u.llm.total.calls), t('gui.set.usg.calls')],
            [cacheUsage(u.llm.total.input_tokens, 0), t('gui.set.usg.in'), u.llm.total.input_missing_calls ? t('gui.set.usg.cache_missing', { n: u.llm.total.input_missing_calls }) : undefined],
            [cacheUsage(u.llm.total.output_tokens, 0), t('gui.set.usg.out'), u.llm.total.output_missing_calls ? t('gui.set.usg.cache_missing', { n: u.llm.total.output_missing_calls }) : undefined],
            [
              usageCost(u.llm.total.cost_usd, 0),
              t('gui.set.usg.cost'),
              u.llm.total.cost_missing_calls ? t('gui.set.usg.cost_missing', { n: u.llm.total.cost_missing_calls }) : undefined,
            ],
            ...(u.llm.total.cache_read_tokens == null ? [] : [[
              cacheUsage(u.llm.total.cache_read_tokens, 0),
              t('gui.set.usg.cache_read'),
              u.llm.total.cache_read_missing_calls ? t('gui.set.usg.cache_missing', { n: u.llm.total.cache_read_missing_calls }) : undefined,
            ] as [string, string, string?]]),
            ...(u.llm.total.cache_write_tokens == null ? [] : [[
              cacheUsage(u.llm.total.cache_write_tokens, 0),
              t('gui.set.usg.cache_write'),
              u.llm.total.cache_write_missing_calls ? t('gui.set.usg.cache_missing', { n: u.llm.total.cache_write_missing_calls }) : undefined,
            ] as [string, string, string?]]),
          ]}
        />
        {u.llm.total.legacy_cost_calls > 0 && <div className="empty-note">{t('gui.set.usg.legacy')}</div>}
        {u.llm.models.length > 0 && (
          <KvList
            prose
            rows={u.llm.models
              .slice(0, 12)
              .map((m) => [
                m.model,
                [
                  `${m.calls} ×`,
                  `${m.input_tokens == null && m.output_tokens == null && m.cache_read_tokens == null && m.cache_write_tokens == null ? t('gui.set.usg.unknown') : fmtTok((m.input_tokens ?? 0) + (m.output_tokens ?? 0) + (m.cache_read_tokens ?? 0) + (m.cache_write_tokens ?? 0))} tok`,
                  usageCost(m.cost_usd, m.cost_missing_calls),
                  m.input_missing_calls ? `${t('gui.set.usg.in')}: ${cacheUsage(m.input_tokens, m.input_missing_calls)}` : '',
                  m.output_missing_calls ? `${t('gui.set.usg.out')}: ${cacheUsage(m.output_tokens, m.output_missing_calls)}` : '',
                  m.cache_read_tokens == null ? '' : `${t('gui.set.usg.cache_read')}: ${cacheUsage(m.cache_read_tokens, m.cache_read_missing_calls)}`,
                  m.cache_write_tokens == null ? '' : `${t('gui.set.usg.cache_write')}: ${cacheUsage(m.cache_write_tokens, m.cache_write_missing_calls)}`,
                ].filter(Boolean).join(' · '),
              ])}
          />
        )}
      </Scard>
      <Scard title={`${t('gui.set.usg.tools')} · ${t('gui.set.usg.window', { d: u.days })}`}>
        {!u.tools.counts.length ? (
          <div className="empty-note">{t('gui.set.nodata')}</div>
        ) : (
          <>
            <StatTiles rows={[[String(u.tools.total), t('gui.set.usg.calls')]]} />
            <KvList rows={u.tools.counts.slice(0, 14).map((x) => [x.name, `${x.count} ×`])} />
          </>
        )}
      </Scard>
    </>
  )
}

/* ---- keyboard -------------------------------------------------------- */

function KeysPage(): JSX.Element {
  const m = modKey()
  const groups: Array<[string, Array<[string, string]>]> = [
    [
      'gui.set.kbd.grp_chat',
      [
        ['gui.set.kbd.send', 'Enter'],
        ['gui.set.kbd.newline', 'Shift + Enter'],
        ['gui.set.kbd.stop', 'Esc'],
      ],
    ],
    [
      'gui.set.kbd.grp_nav',
      [
        ['gui.set.kbd.newtask', `${m} N`],
        ['gui.set.kbd.find', `${m} F`],
        ['gui.set.kbd.rail', `${m} \\`],
      ],
    ],
    [
      'gui.set.kbd.grp_panel',
      [
        ['gui.set.kbd.ws', '1 – 4'],
        ['gui.set.kbd.close', 'Esc'],
      ],
    ],
  ]
  return (
    <Scard>
      <div className="kbdg">
        {groups.map(([gk, rows]) => (
          <div className="g" key={gk}>
            <div className="gt">{t(gk)}</div>
            {rows.map(([k, key]) => (
              <div className="r" key={k}>
                <span>{t(k)}</span>
                <kbd>{key}</kbd>
              </div>
            ))}
          </div>
        ))}
      </div>
    </Scard>
  )
}

/* ---- about ----------------------------------------------------------- */

function AboutPage(): JSX.Element {
  const ver = store.source().version()
  return (
    <>
      <Scard>
        <div className="abtid">
          <div className="nm">
            Raven
            <span className="ver">{ver || '--'}</span>
          </div>
          <button className="mini ghost" onClick={(e) => store.checkUpdate(e.currentTarget)}>
            {t('gui.set.check_update')}
          </button>
        </div>
      </Scard>
      <Scard title={t('gui.set.diag')}>
        <div className="crow">
          <div className="k">{t('gui.set.logs')}</div>
          <div className="c abtlog">
            <code>~/.raven/logs/tui.log</code>
            <CpBtn label={t('gui.set.abt.copy_path')} text="~/.raven/logs/tui.log" />
          </div>
        </div>
        <div className="srow">
          <button className="mini ghost" onClick={() => openUrl('https://raven.evermind.ai')}>
            {t('gui.set.abt.docs')}
          </button>
          <button className="mini ghost" onClick={() => openUrl('https://github.com/EverMind-AI/Raven')}>
            {t('gui.set.abt.repo')}
          </button>
        </div>
      </Scard>
    </>
  )
}

/* ---- model & accounts ------------------------------------------------ */

/* The add-model drawer's three rows, each a name, a glyph out of the shared
   sprite, and the string that labels it.
 *
 * Deliberately short. The registry's vocabulary is thirteen capabilities and
 * five modalities, and a form that asked for all of them is a form nobody
 * fills in -- so it asks the four things a person adding their own deployment
 * actually knows, and derives the rest (see `statedTags`). */
const ADD_TYPES = [
  ['text', 'text', 'gui.model.type.text'],
  ['image', 'image-generation', 'gui.model.type.image'],
  ['embedding', 'embedding', 'gui.model.type.embedding'],
  ['reranker', 'rerank', 'gui.model.type.reranker'],
] as const
const ADD_CAPS = [
  ['reasoning', 'reasoning', 'gui.model.cap.reasoning'],
  ['function-call', 'function-call', 'gui.model.cap.tool'],
] as const
const ADD_IN = [
  ['image', 'image-recognition', 'gui.model.in.vision'],
  ['audio', 'audio-recognition', 'gui.model.in.audio'],
  ['video', 'video-recognition', 'gui.model.in.video'],
] as const

type ModelKind = (typeof ADD_TYPES)[number][0]

/* The buckets `model.fetch_models` sorts a catalogue into, in the order the
   filter row draws them, and the glyph each one wears. Audio and video are
   here and not in the add form: a person adding a model by hand states what it
   reads, while a fetched list has to be able to say what it found. */
const REG_KINDS = ['text', 'image', 'embedding', 'reranker', 'audio', 'video'] as const
const KIND_GLYPH: Record<string, string> = {
  text: 'text',
  image: 'image-generation',
  embedding: 'embedding',
  reranker: 'rerank',
  audio: 'audio-generation',
  video: 'video-generation',
}

/* The vendor a run of rows belongs to: a heading that folds, and -- where
   there is something to add -- one button for the whole vendor.
 *
 * A gateway lists a hundred models under a dozen vendors, and the reader wants
 * one of them. Folding is how the other eleven get out of the way, and adding
 * by the vendor is the shape most of these decisions actually have: all of
 * SiliconFlow's BAAI embedders, none of its voice models. */
function ModelGroup({
  name,
  n,
  open,
  onToggle,
  onAddAll,
  pending = 0,
}: {
  name: string
  n: number
  open: boolean
  onToggle: () => void
  onAddAll?: () => void
  pending?: number
}): JSX.Element {
  return (
    <div className="mgroup">
      <button type="button" className="gt" aria-expanded={open} onClick={onToggle}>
        <span className="cv">⌄</span>
        <span className="gn">{vendorLabel(name)}</span>
        <span className="gc">{n}</span>
      </button>
      {onAddAll && (
        <button
          type="button"
          className="ga"
          disabled={!pending}
          title={t('gui.model.add_group')}
          aria-label={`${t('gui.model.add_group')}: ${vendorLabel(name)}`}
          onClick={onAddAll}
        >
          +
        </button>
      )}
    </div>
  )
}

/* Which groups are folded, by name. A set of the folded ones rather than of
   the open ones: a list arrives open, and a group that appears after a search
   is narrowed should arrive open too. */
function useFolds(): [(name: string) => boolean, (name: string) => () => void] {
  const [folded, setFolded] = useState<string[]>([])
  const isFolded = (name: string): boolean => folded.includes(name)
  const toggle = (name: string) => (): void =>
    setFolded((f) => (f.includes(name) ? f.filter((x) => x !== name) : [...f, name]))
  return [isFolded, toggle]
}

/* An open book: the vendor's model index, next to the list it explains.
 *
 * An icon rather than the words, because the row it sits in already carries a
 * heading and two buttons -- a third piece of text there reads as a fourth
 * control. The name survives on `aria-label` and the tooltip, which is where a
 * screen reader and an unsure reader each look for it. */
function DocsMark(): JSX.Element {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <path d="M8 5.2C7 4.2 5.4 3.8 3.2 3.8v7.6c2.2 0 3.8.4 4.8 1.4 1-1 2.6-1.4 4.8-1.4V3.8c-2.2 0-3.8.4-4.8 1.4z" />
      <path d="M8 5.2v7.6" />
    </svg>
  )
}

/* What this provider will offer, one row per model.
 *
 * `Get model list` is drawn and not wired: it would ask the provider what it
 * serves. It is disabled rather than silently inert -- a button that answers a
 * click with nothing is worse than one that says it cannot yet. `+` opens the
 * drawer, which is the one way to add a model here.
 */
function ModelList({ pv }: { pv: ProviderRow }): JSX.Element {
  /* This section's own list, not the picker's offer: before anything is added
     the offer already holds a curated shortlist, and showing it here read as
     seven models already added to a provider with no key. */
  const models = pv.configured ?? []
  const [isFolded, toggleFold] = useFolds()
  return (
    <div className="msec" data-sec="models">
      <div className="mhead">
        <div className="t">{t('gui.model.models')}</div>
        {pv.docs && (
          <a
            className="mdocs icon"
            href={pv.docs}
            target="_blank"
            rel="noreferrer"
            aria-label={t('gui.model.docs')}
            title={t('gui.model.docs')}
            onClick={(event) => {
              event.preventDefault()
              openUrl(pv.docs as string)
            }}
          >
            <DocsMark />
          </a>
        )}
        <div className="mbtns">
          <button className="mini ghost" onClick={() => void store.fetchModelsOpen(pv.id)}>
            {t('gui.model.get_list')}
          </button>
          <button
            className="mini ghost"
            aria-label={t('gui.model.add_manual')}
            title={t('gui.model.add_manual')}
            onClick={() => store.addModelOpen(pv.id)}
          >
            +
          </button>
        </div>
      </div>
      {!models.length ? (
        <div className="mempty">{t('gui.model.none_yet')}</div>
      ) : (
        <div className="mlist">
          {groupModels(pv.id, models, (m) => m).map(([group, ids]) => (
            <Fragment key={group || '-'}>
              {group && (
                <ModelGroup name={group} n={ids.length} open={!isFolded(group)} onToggle={toggleFold(group)} />
              )}
              {(group && isFolded(group) ? [] : ids).map((m) => {
                const facts = pv.labels?.[m]
                return (
                  <div className="mitem" key={m} title={m}>
                    <ModelIcon vendor={modelGroup(pv.id, m)} model={m} provider={pv.id} name={pv.name} />
                    <span className="nm">{rowName(pv.id, group, m, facts?.label)}</span>
                    <ModelTags facts={facts} />
                    <button
                      className="rm"
                      title={t('gui.model.remove')}
                      aria-label={`${t('gui.model.remove')} ${m}`}
                      onClick={() => void store.providerRun('remove_model', { slug: pv.id, model: m })}
                    >
                      −
                    </button>
                  </div>
                )
              })}
            </Fragment>
          ))}
        </div>
      )}
    </div>
  )
}

/* ── add a model ──────────────────────────────────────────────────────
 *
 * A drawer over the settings dialog rather than a page of its own: adding a
 * model is a detour from reading the provider, and the pane behind it is the
 * context for what is being added.
 */

interface AddForm {
  id: string
  label: string
  kind: ModelKind
  caps: string[]
  ins: string[]
}

/* The tags as the registry spells them, from the four things the form asks.
 *
 * Output modalities are not asked: the kind already answers them -- an
 * embedding model returns vectors, an image model returns images -- and asking
 * twice only creates a pair that can disagree. The modality capabilities go the
 * same way: ticking Vision is what `image-recognition` means, so it is derived
 * rather than offered beside it.
 */
function statedTags(form: AddForm): {
  capabilities: string[]
  input_modalities: string[]
  output_modalities: string[]
} {
  const caps = new Set(form.caps)
  form.ins.forEach((m) => caps.add(`${m}-recognition`))
  if (form.kind === 'embedding') caps.add('embedding')
  if (form.kind === 'reranker') caps.add('rerank')
  if (form.kind === 'image') caps.add('image-generation')
  const outputs = form.kind === 'embedding' ? ['vector'] : form.kind === 'image' ? ['text', 'image'] : ['text']
  return { capabilities: [...caps], input_modalities: ['text', ...form.ins], output_modalities: outputs }
}

function Toggle({
  on,
  glyph,
  label,
  off,
  onClick,
}: {
  on: boolean
  glyph: string
  label: string
  off?: boolean
  onClick: () => void
}): JSX.Element {
  return (
    <button type="button" className="mtog" aria-pressed={on} disabled={off} onClick={onClick}>
      <TagGlyph name={glyph} />
      <span>{label}</span>
    </button>
  )
}

/* An embedding or a reranking model answers with numbers, not a turn: it does
   not reason, it calls no tools, and it reads whatever it indexes as text. So
   picking one of those kinds clears both rows below and closes them -- left
   open, the form could state an embedding model that calls tools, and the
   icon row would then draw it. */
const BARE_KINDS: readonly ModelKind[] = ['embedding', 'reranker']

/* The chrome both drawers wear: a scrim that owns the click-away, a panel from
   the right, a titled header and a footer of verbs. Shared so the two modes
   cannot drift into two different sheets. */
function DrawerShell({
  title,
  who,
  count,
  head,
  foot,
  children,
}: {
  title: string
  who: string
  count?: number
  head?: ReactNode
  foot: ReactNode
  children: ReactNode
}): JSX.Element | null {
  const host = document.getElementById('setModal')
  if (!host) return null
  return createPortal(
    <div className="mdrawer" data-open="true">
      <button className="scrim" aria-label={t('gui.cancel')} onClick={() => store.addModelClose()} />
      <div className="dpane" role="dialog" aria-modal="true" aria-label={title}>
        <header>
          <b>{title}</b>
          {count !== undefined && <span className="ct">{count}</span>}
          <span className="who">{who}</span>
          {head}
          <button className="icb" aria-label={t('gui.close')} onClick={() => store.addModelClose()}>
            ✕
          </button>
        </header>
        {children}
        <footer>{foot}</footer>
      </div>
    </div>,
    host,
  )
}

/* What the provider says it serves, as a list to pick from.
 *
 * Separate from the add-by-hand form on purpose: this one is a catalogue with
 * a search and a filter, and that one is four questions about a model nobody
 * has heard of. They share the drawer and nothing else.
 */
function CatalogueDrawer({ pv, s }: { pv: ProviderRow; s: SettingsState }): JSX.Element | null {
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState('all')
  const [isFolded, toggleFold] = useFolds()
  const { busy, rows, status, error } = s.fetch

  const hay = query.trim().toLowerCase()
  const matched = rows.filter((r) => !hay || r.id.toLowerCase().includes(hay) || r.label.toLowerCase().includes(hay))
  const shown = matched.filter((r) => kind === 'all' || r.kind === kind)
  /* Counted against the search, not against the whole list: a filter row whose
     numbers ignore the term tells the reader nothing about what pressing it
     will show. */
  const counts = (name: string): number => (name === 'all' ? matched.length : matched.filter((r) => r.kind === name).length)
  const pending = shown.filter((r) => !r.added)

  return (
    <DrawerShell
      title={t('gui.model.catalogue_title', { name: pv.name })}
      who={''}
      count={busy ? undefined : rows.length}
      head={
        <button
          className="icb"
          aria-label={t('gui.model.refetch')}
          title={t('gui.model.refetch')}
          disabled={busy}
          onClick={() => void store.fetchModelsOpen(pv.id)}
        >
          ⟳
        </button>
      }
      foot={
        <>
          <span className="pnote">{t('gui.model.catalogue_n', { n: shown.length })}</span>
          <button className="mini ghost" onClick={() => store.addModelClose()}>
            {t('gui.close')}
          </button>
          <button
            className="mini"
            disabled={busy || !pending.length || s.provBusy}
            onClick={() => void store.catalogueAddAll(pv.id, pending)}
          >
            {t('gui.model.add_all', { n: pending.length })}
          </button>
        </>
      }
    >
      <div className="body">
        {busy ? (
          <div className="mempty">{t('gui.model.fetching')}</div>
        ) : !rows.length ? (
          /* Nothing from either source. The probe's own word for why, because a
             blank list on its own says "this provider serves nothing" -- a
             different and much calmer claim than "the key was refused". */
          <div className="perr">{error || status || t('gui.model.none_served')}</div>
        ) : (
          <>
            {status !== 'ok' && (
              /* The rows are real; only their currency is in question. Said as
                 a note rather than an error: a provider with no key yet is the
                 ordinary state of one being set up, not a fault. */
              <div className="mnote">
                {status === 'not_configured'
                  ? t('gui.model.cat_note_key', { name: pv.name })
                  : t('gui.model.cat_note_err', { name: pv.name, why: error || status })}
              </div>
            )}
            <input
              className="msearch"
              type="text"
              value={query}
              placeholder={t('gui.model.catalogue_search')}
              onChange={(e) => setQuery(e.target.value)}
            />
            <div className="mkinds">
              {['all', ...REG_KINDS].map((name) => (
                <button
                  key={name}
                  type="button"
                  className="mkind"
                  aria-pressed={kind === name}
                  onClick={() => setKind(name)}
                >
                  {name !== 'all' && <TagGlyph name={KIND_GLYPH[name] as string} />}
                  <span>{t(name === 'all' ? 'gui.model.kind_all' : `gui.model.type.${name}`)}</span>
                  <i>{counts(name)}</i>
                </button>
              ))}
            </div>
            {!shown.length ? (
              <div className="mempty">{t(rows.length ? 'gui.model.no_match' : 'gui.model.none_served')}</div>
            ) : (
              <div className="mlist">
                {groupModels(pv.id, shown, (row) => row.id).map(([group, rows]) => (
                  <Fragment key={group || '-'}>
                    {group && (
                      <ModelGroup
                        name={group}
                        n={rows.length}
                        open={!isFolded(group)}
                        onToggle={toggleFold(group)}
                        pending={rows.filter((row) => !row.added).length}
                        onAddAll={() => void store.catalogueAddAll(pv.id, rows.filter((row) => !row.added))}
                      />
                    )}
                    {(group && isFolded(group) ? [] : rows).map((row) => (
                      <div className="mitem" key={row.id} title={row.id} data-on={row.added || undefined}>
                        <ModelIcon vendor={modelGroup(pv.id, row.id)} model={row.id} provider={pv.id} name={pv.name} />
                        <span className="nm">{rowName(pv.id, group, row.id, row.label)}</span>
                        <ModelTags facts={row} />
                        <button
                          className="rm"
                          disabled={s.provBusy}
                          title={t(row.added ? 'gui.model.remove' : 'gui.add')}
                          aria-label={`${t(row.added ? 'gui.model.remove' : 'gui.add')} ${row.id}`}
                          onClick={() => void store.catalogueToggle(pv.id, row)}
                        >
                          {row.added ? '−' : '+'}
                        </button>
                      </div>
                    ))}
                  </Fragment>
                ))}
              </div>
            )}
            {s.provErr && <div className="perr">{s.provErr}</div>}
          </>
        )}
      </div>
    </DrawerShell>
  )
}

function AddModelDrawer({ pv, s }: { pv: ProviderRow; s: SettingsState }): JSX.Element | null {
  const [form, setForm] = useState<AddForm>({ id: '', label: '', kind: 'text', caps: [], ins: [] })
  const field = useRef<HTMLInputElement>(null)
  useEffect(() => {
    field.current?.focus()
  }, [])

  const bare = BARE_KINDS.includes(form.kind)
  const flip = (key: 'caps' | 'ins', value: string) => () =>
    setForm((f) => ({
      ...f,
      [key]: f[key].includes(value) ? f[key].filter((x) => x !== value) : [...f[key], value],
    }))
  const pickKind = (kind: ModelKind) => () =>
    setForm((f) => (BARE_KINDS.includes(kind) ? { ...f, kind, caps: [], ins: [] } : { ...f, kind }))
  const submit = (): void => {
    const id = form.id.trim()
    if (!id) return
    void store.addModelSave({
      slug: pv.id,
      model: id,
      ...(form.label.trim() ? { label: form.label.trim() } : {}),
      ...statedTags(form),
    })
  }

  return (
    <DrawerShell
      title={t('gui.model.add_title')}
      who={pv.name}
      foot={
        <>
          <button className="mini ghost" onClick={() => store.addModelClose()}>
            {t('gui.cancel')}
          </button>
          <button className="mini" disabled={!form.id.trim() || s.provBusy} onClick={submit}>
            {t('gui.model.add_title')}
          </button>
        </>
      }
    >
      <div className="body">
        <label className="fld">
          <span className="k">
            {t('gui.model.add_id')} <i>*</i>
          </span>
          <input
            ref={field}
            type="text"
            value={form.id}
            placeholder={t('gui.model.add_id_ph')}
            onChange={(e) => setForm((f) => ({ ...f, id: e.target.value }))}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submit()
            }}
          />
        </label>
        <label className="fld">
          <span className="k">{t('gui.model.add_name')}</span>
          <input
            type="text"
            value={form.label}
            placeholder={t('gui.model.add_name_ph')}
            onChange={(e) => setForm((f) => ({ ...f, label: e.target.value }))}
          />
        </label>

        <div className="grp">
          <div className="gt">{t('gui.model.add_type')}</div>
          {/* One of four, not a set: a model is one kind of thing, and the
              kind is what says whether the answer is prose or vectors. */}
          <div className="togs">
            {ADD_TYPES.map(([name, glyph, key]) => (
              <Toggle key={name} on={form.kind === name} glyph={glyph} label={t(key)} onClick={pickKind(name)} />
            ))}
          </div>
        </div>

        <div className="grp" data-off={bare || undefined}>
          <div className="gt">{t('gui.model.add_caps')}</div>
          <div className="togs">
            {ADD_CAPS.map(([name, glyph, key]) => (
              <Toggle
                key={name}
                on={form.caps.includes(name)}
                glyph={glyph}
                label={t(key)}
                off={bare}
                onClick={flip('caps', name)}
              />
            ))}
          </div>
        </div>

        <div className="grp" data-off={bare || undefined}>
          <div className="gt">{t('gui.model.add_in')}</div>
          <div className="togs">
            {ADD_IN.map(([name, glyph, key]) => (
              <Toggle
                key={name}
                on={form.ins.includes(name)}
                glyph={glyph}
                label={t(key)}
                off={bare}
                onClick={flip('ins', name)}
              />
            ))}
          </div>
        </div>
        {s.addErr && <div className="perr">{s.addErr}</div>}
      </div>
    </DrawerShell>
  )
}

/* The right-hand pane: one provider's credentials and its model list.
 *
 * The credentials keep the shape the accordion card had -- an API key, a host
 * where the provider needs one, a login command where it takes no key -- but
 * they are now the pane rather than a fold, so nothing has to be opened to see
 * what a provider is set to.
 */
function ProvPanel({ pv, s }: { pv: ProviderRow; s: SettingsState }): JSX.Element {
  const base = useRef<HTMLInputElement>(null)
  const key = useRef<HTMLInputElement>(null)
  const [copied, setCopied] = useState(false)
  /* Tracked rather than read off the ref at render: the button's disabled state
     has to follow the field, and an uncontrolled input's value changes without
     telling React. */
  const [typedKey, setTypedKey] = useState(false)
  /* The backend answers this: a local server that can sit behind a token has a
     key field, an address-only one does not. Matching on the slug here is how
     the pane and the wizard came to disagree about the second such provider. */
  const acceptsApiKey = pv.acceptsKey ?? pv.kind !== 'local'
  const initialBase = pv.apiBase || pv.defaultApiBase || ''
  /* An address this provider cannot be reached without: the credential gate
     wants it in every submission, default or not. */
  const baseRequired = pv.needsBase || pv.kind === 'endpoint'
  const save = (): void => {
    const params: Record<string, unknown> = { slug: pv.id }
    if (acceptsApiKey) params.api_key = key.current?.value.trim() ?? ''
    const typedBase = base.current?.value.trim() ?? ''
    /* Only what the person actually chose gets written. A shipped default is
       shown so the pane can answer "where does this go", but several of them
       are display-only on purpose -- dashscope's compatible-mode address is
       one LiteLLM's own driver must not be handed -- so saving the field
       unchanged would silently turn a label into an override and break the
       route it was only describing. */
    if (typedBase && (baseRequired || typedBase !== initialBase)) params.api_base = typedBase
    if (pv.kind === 'local' && !params.api_base) {
      store.provSay(t('gui.model.need_base'))
      return
    }
    if (pv.kind !== 'local' && !params.api_key) {
      store.provSay(t('gui.model.need_key'))
      return
    }
    void store.providerRun('save_key', params)
  }
  /* Every provider that has an address shows it, not only the ones that cannot
     work without one: "what is this pointed at" is a question asked of a
     working provider as often as a broken one, and the answer was previously
     visible for three of them. Providers with no address of their own (the
     vendors LiteLLM reaches by SDK default) still show no field -- there is
     nothing to put in it. */
  const showBase = baseRequired || !!initialBase
  const keyRequired = acceptsApiKey && pv.kind !== 'local'
  return (
    <div className="mpanel">
      <div className="mtitle">
        <ProviderIcon id={pv.id} name={pv.name} />
        <ProviderLink homepage={pv.homepage} name={pv.name} />
        {pv.id === s.snap.curProvider && <span className="tagm">{t('gui.model.is_default')}</span>}
        <ProviderStatus connected={pv.on} label={t(pv.on ? 'gui.model.state.connected' : 'gui.model.not_connected')} />
      </div>
      {/* What the card used to say under the name: how this provider is
          reached, and how much it lends the picker. The fields below imply the
          first only once you know what a login command means. */}
      <div className="msub">{summary(pv).join(' · ')}</div>
      <div className="pform">
        {pv.kind === 'oauth' ? (
          <>
            <div className="pnote">{t('gui.model.oauth_hint', { name: pv.name })}</div>
            <div className="cmd">
              <code>{loginCmd(pv.id)}</code>
              <button
                className="mini ghost"
                onClick={() => {
                  if (navigator.clipboard) void navigator.clipboard.writeText(loginCmd(pv.id))
                  setCopied(true)
                  setTimeout(() => setCopied(false), 1200)
                }}
              >
                {t(copied ? 'gui.model.copied' : 'gui.model.copy')}
              </button>
            </div>
          </>
        ) : (
          <>
            {acceptsApiKey && (
              <div className="msec" data-sec="key">
                <div className="mhead">
                  <div className="t">{t('gui.model.api_key')}</div>
                  {pv.homepage && (
                    <a
                      className="mdocs"
                      href={pv.homepage}
                      target="_blank"
                      rel="noreferrer"
                      onClick={(event) => {
                        event.preventDefault()
                        openUrl(pv.homepage as string)
                      }}
                    >
                      {t('gui.model.get_key')}
                    </a>
                  )}
                </div>
                <div className="keyrow">
                  <KeyInput
                    ref={key}
                    placeholder={pv.on ? t('gui.model.key_ph_update') : t('gui.model.key_ph', { name: pv.name })}
                    aria-label={`${pv.name} API Key`}
                    onInput={(event) => setTypedKey(!!event.currentTarget.value.trim())}
                  />
                  {/* Off until there is a key to send -- the same condition the
                      save refuses on, said before the click rather than after
                      it. Not for a local server that merely accepts a token:
                      there an empty field is a complete submission, and the
                      address is what gets saved. */}
                  <button className="mini" disabled={keyRequired && !typedKey} onClick={save}>
                    {t(pv.on ? 'gui.model.update' : 'gui.model.connect')}
                  </button>
                </div>
                {pv.env && <div className="pnote">{t('gui.model.env_hint', { env: pv.env })}</div>}
              </div>
            )}
            {showBase && (
              <div className="msec" data-sec="host">
                <div className="mhead">
                  <div className="t">{t('gui.model.api_host')}</div>
                </div>
                <div className="keyrow">
                  <HostInput
                    inputRef={base}
                    value={initialBase}
                    placeholder={t(pv.kind === 'local' ? 'gui.model.base_ph_local' : 'gui.model.base_ph')}
                  />
                  {/* The host icon restores the initial value rather than saving
                      a separate action -- the address travels with the key, on
                      the button above. */}
                  <button
                    className="mini update"
                    type="button"
                    aria-label={t('gui.model.update')}
                    title={t('gui.model.update')}
                    onClick={() => {
                      if (base.current) base.current.value = initialBase
                    }}
                  >
                    <UpdateIcon />
                  </button>
                </div>
              </div>
            )}
          </>
        )}
        <ModelList pv={pv} />
        {pv.on && (
          <button
            className="mini ghost danger"
            style={{ justifySelf: 'start' }}
            onClick={() =>
              shell().confirmAsk(
                t('gui.model.disconnect_title'),
                t('gui.model.disconnect_body', { name: pv.name }),
                t('gui.model.disconnect_title'),
                () => void store.providerRun('disconnect', { slug: pv.id }),
              )
            }
          >
            {t('gui.model.disconnect')}
          </button>
        )}
        {s.provErr && <div className="perr">{s.provErr}</div>}
      </div>
    </div>
  )
}

/* One row of the left rail. A button, not a card: the whole row is the target,
   and the homepage link that used to sit in the name moved to the pane header
   -- an anchor inside a button is neither valid nor clickable in the way
   either element promises. */
function ProvRow({ pv, s, selected }: { pv: ProviderRow; s: SettingsState; selected: boolean }): JSX.Element {
  return (
    <button className="mrow" type="button" aria-current={selected} onClick={() => store.provSelect(pv.id)}>
      <ProviderIcon id={pv.id} name={pv.name} />
      <span className="nm">{pv.name}</span>
      {pv.id === s.snap.curProvider && <span className="tagm">{t('gui.model.is_default')}</span>}
      <ProviderStatus connected={pv.on} label={t(pv.on ? 'gui.model.state.connected' : 'gui.model.not_connected')} />
    </button>
  )
}

/* Vendors the rail lists more than once. MiniMax is four sections -- global and
   CN, each reachable by key and by OAuth -- and the rail is read to find a
   vendor, not a credential shape, so four rows of one name is four times the
   scanning for the same answer.

   Declared rather than derived from a shared slug prefix: `openai` and
   `openai_codex` share one too, and those are two things a reader picks
   between rather than one thing with variants. Members are listed in the order
   they should appear inside the group. */
const RAIL_GROUPS: ReadonlyArray<{ key: string; label: string; members: readonly string[] }> = [
  { key: 'minimax', label: 'MiniMax', members: ['minimax', 'minimax_cn_api', 'minimax_global', 'minimax_cn'] },
]

type RailEntry =
  | { kind: 'one'; key: string; pv: ProviderRow }
  | { kind: 'group'; key: string; label: string; rows: ProviderRow[] }

/* The rail's rows, with each declared family collapsed into one entry.
 *
 * A family whose other members are missing is not a family: hiding a lone
 * provider behind a fold costs a click and saves nothing. */
function railEntries(providers: ProviderRow[]): RailEntry[] {
  const byId = new Map(providers.map((pv) => [pv.id, pv]))
  const done = new Set<string>()
  const entries: RailEntry[] = []
  for (const pv of providers) {
    const group = RAIL_GROUPS.find((g) => g.members.includes(pv.id))
    if (!group) {
      entries.push({ kind: 'one', key: pv.id, pv })
      continue
    }
    if (done.has(group.key)) continue
    done.add(group.key)
    const rows = group.members.map((id) => byId.get(id)).filter((row): row is ProviderRow => !!row)
    if (rows.length < 2) entries.push({ kind: 'one', key: rows[0]!.id, pv: rows[0]! })
    else entries.push({ kind: 'group', key: group.key, label: group.label, rows })
  }
  return entries
}

const entryRows = (entry: RailEntry): ProviderRow[] => (entry.kind === 'one' ? [entry.pv] : entry.rows)

/* One folded family in the rail. Reads as a provider row -- same mark, same
   name, same dot -- because that is what it stands in for while closed. The
   dot lights when any member is connected: the question the rail answers is
   "can I use MiniMax", and which of the four sections carries the credential
   is the pane's business, not the rail's. */
function ProvGroup({
  entry,
  s,
  open,
  onToggle,
  selectedId,
}: {
  entry: Extract<RailEntry, { kind: 'group' }>
  s: SettingsState
  open: boolean
  onToggle: () => void
  selectedId?: string
}): JSX.Element {
  const on = entry.rows.some((pv) => pv.on)
  const holdsDefault = entry.rows.some((pv) => pv.id === s.snap.curProvider)
  return (
    <div className="mgrp">
      <button className="mrow gh" type="button" aria-expanded={open} onClick={onToggle}>
        <span className="cv">⌄</span>
        <ProviderIcon id={entry.rows[0]!.id} name={entry.label} />
        <span className="nm">{entry.label}</span>
        <span className="gc">{entry.rows.length}</span>
        {/* Only while closed: the member row says it better when it is visible. */}
        {holdsDefault && !open && <span className="tagm">{t('gui.model.is_default')}</span>}
        <ProviderStatus connected={on} label={t(on ? 'gui.model.state.connected' : 'gui.model.not_connected')} />
      </button>
      {open && (
        <div className="mgsub">
          {entry.rows.map((pv) => (
            <ProvRow key={pv.id} pv={pv} s={s} selected={pv.id === selectedId} />
          ))}
        </div>
      )}
    </div>
  )
}

/* Providers on the left, the selected one's settings on the right.
 *
 * Connected first, because "which of these can I actually use" is the question
 * this page is opened with -- and stably within each half, so the rail does not
 * reshuffle under the cursor when a key is saved. A family rises as a unit on
 * the same rule: splitting MiniMax across the two halves puts the same vendor
 * in two places, which is the thing grouping it was meant to stop. */
function ProvSplit({ s }: { s: SettingsState }): JSX.Element {
  const entries = railEntries(s.snap.providers)
  const connected = (entry: RailEntry): boolean => entryRows(entry).some((pv) => pv.on)
  const ordered = [...entries.filter(connected), ...entries.filter((entry) => !connected(entry))]
  const rows = ordered.flatMap(entryRows)
  /* Resolved rather than stored: before anything is picked the pane shows the
     first connected provider, and a provider that disappears from the list
     must not leave the pane empty. */
  const selected = rows.find((p) => p.id === s.provOpen) ?? rows.find((p) => p.on) ?? rows[0]
  /* Closed is the point of the fold, so the set starts empty -- except for the
     family holding whatever the pane opens on, which would otherwise be
     selected and invisible. Seeded once: after that the fold is the reader's,
     including folding away the row they are looking at. */
  const [openGroups, setOpenGroups] = useState<string[]>(() =>
    ordered
      .filter((entry) => entry.kind === 'group' && entry.rows.some((pv) => pv.id === selected?.id))
      .map((entry) => entry.key),
  )
  const toggleGroup = (key: string) => (): void =>
    setOpenGroups((keys) => (keys.includes(key) ? keys.filter((k) => k !== key) : [...keys, key]))

  useEffect(() => {
    if (!s.provFocus) return
    store.clearProvFocus()
    const field = document.querySelector<HTMLInputElement>('.mpanel .pform input')
    if (field) field.focus()
  })

  if (!rows.length) return <div className="pnote">{t('gui.model.none_connected')}</div>
  return (
    <div className="msplit">
      <ModelTagDefs />
      <div className="mrail">
        {ordered.map((entry) =>
          entry.kind === 'one' ? (
            <ProvRow key={entry.key} pv={entry.pv} s={s} selected={entry.pv.id === selected?.id} />
          ) : (
            <ProvGroup
              key={entry.key}
              entry={entry}
              s={s}
              open={openGroups.includes(entry.key)}
              onToggle={toggleGroup(entry.key)}
              {...(selected ? { selectedId: selected.id } : {})}
            />
          ),
        )}
      </div>
      {selected ? <ProvPanel key={selected.id} pv={selected} s={s} /> : null}
    </div>
  )
}

/* Sampling and routing sit with the model: every knob here is "how this
   model answers". The three ceilings stay read-only facts behind a fold. */
function ModelTuning({ s }: { s: SettingsState }): JSX.Element {
  const raw = s.snap.raw
  return (
    <>
      <Scard title={t('gui.set.mdl.tuning')}>
        <WPick
          k="agents.defaults.reasoningEffort"
          opts={[
            ['minimal', t('gui.set.mdl.eff_min')],
            ['low', t('gui.set.mdl.eff_low')],
            ['medium', t('gui.set.mdl.eff_med')],
            ['high', t('gui.set.mdl.eff_high')],
          ]}
          val={String(V(raw, 'agents.defaults.reasoningEffort', 'medium'))}
        />
      </Scard>
      <button className="foldrow" aria-expanded={s.mdlAdv} onClick={() => store.advToggle()}>
        {t(s.mdlAdv ? 'gui.set.mdl.adv_hide' : 'gui.set.mdl.adv')}
      </button>
      {s.mdlAdv && (
        <Scard title={t('gui.set.mdl.limits')}>
          <KvList
            rows={[
              [t('gui.set.mdl.maxtok'), String(V(raw, 'agents.defaults.maxTokens', 8192))],
              [t('gui.set.mdl.ctx'), String(V(raw, 'agents.defaults.contextWindowTokens', 65536))],
              [t('gui.set.mdl.iter'), String(V(raw, 'agents.defaults.maxToolIterations', 40))],
            ]}
          />
        </Scard>
      )}
    </>
  )
}

function ModelPage({ s }: { s: SettingsState }): JSX.Element {
  const [nl, say] = useNl()
  return (
    <>
      <div className="scard inline">
        <div className="ch">
          <div className="t">{t('gui.model.default')}</div>
        </div>
        <button
          className="mini ghost pickm"
          onClick={(e) => {
            if (!store.pickDefault(e.currentTarget)) say()
          }}
        >
          <span className="mono">{shortModel(s.snap.model) || t('gui.model.unset')}</span>
          <span className="car">⌄</span>
        </button>
        {nl && <div className="nlmsg">{nl}</div>}
      </div>
      <ProvSplit s={s} />
      <ModelTuning s={s} />
    </>
  )
}

/* ---- permission ------------------------------------------------------ */

function PermPage({ s }: { s: SettingsState }): JSX.Element {
  const raw = s.snap.raw
  const sh = shell()
  const sb = String(V(raw, 'tools.sandbox.backend', 'none'))
  const sbName = sb === 'none' ? t('gui.set.prm.sb_none') : sb === 'auto' ? t('gui.set.prm.sb_auto') : sb
  return (
    <>
      <Scard title={t('gui.set.prm.mode')} desc={t('gui.set.prm.mode_note')}>
        <WPick
          k="permissions.mode"
          opts={[
            ['ask', t('gui.perm.ask'), t('gui.perm.ask_h')],
            ['smart', t('gui.perm.smart'), t('gui.perm.smart_h')],
            ['full', t('gui.perm.full'), t('gui.perm.full_h')],
          ]}
          val={String(V(raw, 'permissions.mode', 'ask'))}
        />
      </Scard>
      <Scard title={t('gui.set.prm.guard')}>
        <KvList
          rows={[
            [t('gui.set.prm.workspace'), onoff(V(raw, 'tools.restrictToWorkspace', false) === true)],
            [t('gui.set.prm.sandbox'), sbName, sb === 'none' ? 'unset' : 'ok'],
          ]}
        />
        <Srmk configPath={s.snap.configPath} />
      </Scard>
    </>
  )
}

/* ---- memory ---------------------------------------------------------- */

const MEM_ROLES: Array<[string, string, boolean]> = [
  ['llm', 'gui.set.mem.role_llm', true],
  ['embedding', 'gui.set.mem.role_embedding', true],
  ['rerank', 'gui.set.mem.role_rerank', false],
  ['multimodal', 'gui.set.mem.role_multimodal', false],
]

function MemRole({
  sec,
  labelKey,
  required,
  cur,
  open,
  say,
  lenders,
}: {
  sec: string
  labelKey: string
  required: boolean
  cur: EverosSection
  open: boolean
  say: () => void
  lenders: ProviderRow[]
}): JSX.Element {
  const model = useRef<HTMLInputElement>(null)
  const base = useRef<HTMLInputElement>(null)
  const key = useRef<HTMLInputElement>(null)
  /* Empty means "I will type the address and key myself", which is what this
     row always was. Picking a lender hides both, because the server fills them
     from that provider and a field the reader can edit but that is overwritten
     on save is a lie about who decides. */
  const [borrow, setBorrow] = useState('')
  const on = !!(cur.model && cur.api_key_set)
  const save = (): void => {
    const fields: Record<string, string> = {}
    if (model.current?.value.trim()) fields.model = model.current.value.trim()
    /* No `borrow` guard on these two: picking a lender unmounts both inputs,
       so their refs are null and there is nothing to read. One mechanism,
       and it is the one the reader can see. */
    if (base.current?.value.trim()) fields.base_url = base.current.value.trim()
    if (key.current?.value.trim()) fields.api_key = key.current.value.trim()
    if (!Object.keys(fields).length && !borrow) {
      model.current?.focus()
      return
    }
    void store.everosSave(sec, fields, borrow || undefined).then((r) => {
      if (r === 'notlive') say()
    })
  }
  return (
    <div className="mrole">
      <div className="hd">
        <span className={'kchip' + (on ? '' : ' off')} title={t(on ? 'gui.set.tls.key_set' : 'gui.set.tls.key_unset')}>
          <span className="led" />
          <span>{t(labelKey)}</span>
        </span>
        <span className={'mo' + (cur.model ? '' : ' dim')}>{cur.model || t('gui.set.unset')}</span>
        <button className="mini ghost" onClick={() => store.memEditSet(sec)}>
          {t(open ? 'gui.set.mem.fold' : on ? 'gui.model.update' : 'gui.set.mem.setup')}
        </button>
      </div>
      {open && (
        <div className="ff">
          <input ref={model} type="text" defaultValue={cur.model || ''} placeholder={t('gui.set.mem.model_ph')} />
          {lenders.length > 0 && (
            <select className="mlend" value={borrow} onChange={(e) => setBorrow(e.currentTarget.value)}>
              <option value="">{t('gui.set.mem.own_key')}</option>
              {lenders.map((pv) => (
                <option key={pv.id} value={pv.id}>{t('gui.set.mem.borrow_from', { name: pv.name })}</option>
              ))}
            </select>
          )}
          {borrow ? (
            <div className="mnote">{t('gui.set.mem.borrow_note')}</div>
          ) : (
            <>
              <input ref={base} type="text" defaultValue={cur.base_url || ''} placeholder="https://api.example.com/v1" />
              <KeyInput ref={key} placeholder={cur.api_key_set ? t('gui.set.tls.key_set') : 'API Key'} aria-label="API Key" />
            </>
          )}
          <div className="mfacts">
            <button className="mini" onClick={save}>
              {t('gui.set.mem.save')}
            </button>
            {!required && (cur.model || cur.api_key_set) && (
              <button
                className="mini ghost"
                onClick={() =>
                  void store.everosSave(sec, null).then((r) => {
                    if (r === 'notlive') say()
                  })
                }
              >
                {t('gui.set.mem.disable')}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function MemoryPage({ s }: { s: SettingsState }): JSX.Element {
  const raw = s.snap.raw
  const [nl, say] = useNl()
  const secs = (s.snap.everos && s.snap.everos.sections) || {}
  /* Only the ones with a key to lend, which is narrower than `on`. That flag
     is `credential_status(...).ok` -- "this provider is usable" -- and two
     kinds satisfy it with no key at all: oauth is authenticated by a token
     file, and a local deployment by an address. Offering either is a choice
     that fails on save for a reason the row cannot show. */
  const lenders = (s.snap.providers || []).filter(
    (pv) => pv.on && pv.kind !== 'oauth' && pv.kind !== 'local',
  )
  return (
    <>
      <Scard title={t('gui.set.memory')}>
        <NumRow label={t('gui.set.mem.topk')} k="memory.memoryTopK" val={Number(V(raw, 'memory.memoryTopK', 5))} min={1} max={50} />
        <SwiRow
          label={t('gui.set.mem.learn')}
          k="agents.defaults.enablePersonalization"
          on={V(raw, 'agents.defaults.enablePersonalization', false) === true}
        />
      </Scard>
      {/* The backend is not a choice: long-term memory runs on EverOS. What a
          person configures is EverOS itself -- the model behind each role. */}
      <div className="scard">
        <div className="ch">
          <div className="t">{t('gui.set.mem.models')}</div>
        </div>
        <div className="fset">
          {MEM_ROLES.map(([sec, key, required]) => (
            <MemRole
              key={sec}
              sec={sec}
              labelKey={key}
              required={required}
              cur={secs[sec] || {}}
              open={s.memEdit === sec}
              say={say}
              lenders={lenders}
            />
          ))}
        </div>
        {nl && <div className="nlmsg">{nl}</div>}
      </div>
    </>
  )
}

/* ---- proactivity / exec / channel / data ----------------------------- */

function ProactPage(): JSX.Element {
  const sh = shell()
  return (
    <>
      <Scard>
        <div className="sempty">
          <div className="h">{t('gui.set.pro.hero')}</div>
        </div>
      </Scard>
      <Scard title={t('gui.set.pro.channels')}>
        <button
          className="mini ghost"
          onClick={() => {
            sh.closeSet?.()
            openConn()
          }}
        >
          {t('gui.set.chn.manage')}
        </button>
      </Scard>
    </>
  )
}

function ExecPage({ s }: { s: SettingsState }): JSX.Element {
  const raw = s.snap.raw
  /* The proxy is display-only for the same reason the sandbox backend is:
     it routes every WebSearch and WebFetch, API keys and all. */
  const proxy = Vnull(raw, 'tools.web.proxy', '') as string
  return (
    <Scard title={t('gui.set.exe.card')}>
      <KvList
        rows={[
          [t('gui.set.cwd'), String(V(raw, 'agents.defaults.workspace', '~/.raven/workspace'))],
          [t('gui.set.exe.path'), String(V(raw, 'tools.exec.pathAppend', '')) || t('gui.set.unset'), V(raw, 'tools.exec.pathAppend', '') ? '' : 'unset'],
          [t('gui.set.exe.proxy'), String(proxy || '') || t('gui.set.unset'), proxy ? '' : 'unset'],
        ]}
      />
      <NumRow label={t('gui.set.prm.timeout')} k="tools.exec.timeout" val={Number(V(raw, 'tools.exec.timeout', 60))} min={5} max={3600} />
      <Srmk configPath={s.snap.configPath} />
    </Scard>
  )
}

function ChannelPage({ s }: { s: SettingsState }): JSX.Element {
  const sh = shell()
  const raw = s.snap.raw
  const fwd = (V(raw, 'cron.forwardChannels', []) as string[]) || []
  return (
    <>
      <Scard title={t('gui.set.chn.card')}>
        <SwiRow label={t('gui.set.chn.progress')} k="channels.sendProgress" on={V(raw, 'channels.sendProgress', true) === true} />
        <SwiRow label={t('gui.set.chn.hints')} k="channels.sendToolHints" on={V(raw, 'channels.sendToolHints', false) === true} />
        <TextRow
          label={t('gui.set.chn.cron_to')}
          k="cron.forwardChannels"
          val={fwd.join(', ')}
          ph={t('gui.set.chn.cron_ph')}
          norm={(v) =>
            v
              .split(',')
              .map((x) => x.trim())
              .filter(Boolean)
          }
        />
        <TextRow
          label={t('gui.set.chn.tz')}
          k="cron.defaultTimezone"
          val={String(V(raw, 'cron.defaultTimezone', 'Asia/Shanghai'))}
          ph="Asia/Shanghai"
          norm={(v) => (v.trim() ? v.trim() : undefined)}
        />
      </Scard>
      <Scard title={t('gui.set.chn.entry')}>
        <button
          className="mini ghost"
          onClick={() => {
            sh.closeSet?.()
            openConn()
          }}
        >
          {t('gui.set.chn.manage')}
        </button>
      </Scard>
    </>
  )
}

function DataPage({ s }: { s: SettingsState }): JSX.Element {
  const sh = shell()
  const cp = s.snap.configPath
  return (
    <>
      <Scard title={t('gui.set.dat.where')}>
        <KvList
          rows={[
            [t('gui.set.dat.config'), cp],
            [t('gui.set.store'), String(V(s.snap.raw, 'agents.defaults.workspace', '~/.raven/workspace'))],
          ]}
        />
        <div className="srow">
          <CpBtn label={t('gui.set.abt.copy_path')} text={cp} />
        </div>
      </Scard>
      {/* The destructive action gets its own card and its own colour. */}
      <Scard title={t('gui.set.danger')}>
        <button
          className="mini ghost danger"
          onClick={() =>
            /* A session operation offered from the settings page, so it is
               the session source's, not this page's. */
            sh.confirmAsk(t('gui.set.delete_all'), t('gui.set.delete_all_body', { n: sessionCount() }), t('gui.set.delete_all_yes'), () =>
              deleteAllSessions(),
            )
          }
        >
          {t('gui.set.delete_all')}
        </button>
      </Scard>
    </>
  )
}

/* ---- assembly -------------------------------------------------------- */

/* ---- the built-in tool inventory ------------------------------------------
   Drawn here rather than handed back to the legacy capabilities module, which
   is what the `renderToolset` shell verb used to do: the panel belongs to this
   dialog, and a page that lends its own panel out cannot be read on its own.

   Tools are a fixed inventory the agent ships with, not a store -- which is
   why they sit under the agent here and not in a module for adding and
   removing things.

   Which tools take a credential, and where it is stored. These used to be a
   separate settings page of four unexplained key fields; a key belongs on the
   tool it unlocks, where "set / not set" reads next to the switch it gates. */
const TOOL_CRED: Record<string, string> = {
  image_generate: 'tools.media.image.apiKey',
  deep_research: 'tools.deepResearch.apiKey',
}

/* The two web tools route through a vendor the user picks, and the vendor
   decides which key slot the tool reads (tools.web.providers.<vendor>.apiKey).
   Mirrors WebSearchProvider / WebFetchProvider in raven/config/schema.py. */
interface WebVendorPick {
  path: string
  vendors: string[]
  fallback: string
}
const WEB_VENDOR: Record<string, WebVendorPick> = {
  web_search: {
    path: 'tools.web.search.provider',
    vendors: ['serper', 'anysearch', 'serpapi', 'tavily', 'exa', 'brave', 'firecrawl'],
    fallback: 'serper',
  },
  web_fetch: {
    path: 'tools.web.fetch.provider',
    vendors: ['jina', 'anysearch', 'tavily', 'exa', 'firecrawl'],
    fallback: 'jina',
  },
}
const WEB_VENDOR_LABEL: Record<string, string> = {
  serper: 'Serper',
  anysearch: 'AnySearch',
  serpapi: 'SerpApi',
  jina: 'Jina Reader',
  tavily: 'Tavily',
  exa: 'Exa',
  brave: 'Brave Search',
  firecrawl: 'Firecrawl',
}
function webVendor(id: string, raw: Record<string, unknown>): string {
  const pick = WEB_VENDOR[id]!
  return String(V(raw, pick.path, pick.fallback))
}
function toolCred(id: string, raw: Record<string, unknown>): string | undefined {
  if (WEB_VENDOR[id]) return `tools.web.providers.${webVendor(id, raw)}.apiKey`
  return TOOL_CRED[id]
}
/* The pre-vendor leaf this vendor's key may still sit in, or undefined. A
   config written before the vendor layout holds its key there and the tools
   still read it, after the slot -- so the row must count it as configured and
   Clear must retire it. Named once, because a reader that knows about the leaf
   and a writer that does not is how a cleared credential stays live. */
function legacyCred(id: string, raw: Record<string, unknown>): string | undefined {
  /* The id is tested before the vendor is asked: `webVendor` reads a table
     keyed by web tool, and every other tool reaches here too. */
  if (id === 'web_search' && webVendor(id, raw) === 'serper') return 'tools.web.search.apiKey'
  if (id === 'web_fetch' && webVendor(id, raw) === 'jina') return 'tools.web.jinaApiKey'
  return undefined
}
function credOn(id: string, raw: Record<string, unknown>, cred: string): boolean {
  if (V(raw, cred, '')) return true
  const legacy = legacyCred(id, raw)
  return !!(legacy && V(raw, legacy, ''))
}

function ToolLine({ row, raw, s }: { row: ToolRow; raw: Record<string, unknown>; s: SettingsState }): JSX.Element {
  const cred = toolCred(row.id, raw)
  /* The key's state rides on the row itself: a switched-on tool with no key
     is the gap this chip exists to make visible. */
  const keyOn = !!cred && credOn(row.id, raw, cred)
  return (
    <div className={'trow' + (row.on ? '' : ' off')}>
      <div className="nm">
        <span>{row.name}</span>
        {row.danger && (
          <span className="tag warn" style={{ fontSize: '10px' }}>
            {t('gui.caps.mutates')}
          </span>
        )}
        {cred && (
          <span className={'kchip' + (keyOn ? '' : ' off')} style={{ fontSize: '10px' }}>
            <span className="led" />
            <span>{t(keyOn ? 'gui.set.tls.key_set' : 'gui.set.tls.key_unset')}</span>
          </span>
        )}
      </div>
      <div className="one" title={row.one}>
        {row.one}
      </div>
      <div className="bdgs">
        <span className={'kd' + (row.reach === 'auth' ? ' auth' : '')} title={reachHint(row.reach)}>
          {reachText(row.reach)}
        </span>
      </div>
      <div className="ctl">
        {cred && (
          <button className="mini ghost" onClick={() => store.toolKeyToggle(row.id)}>
            {t(s.toolKeyEdit === row.id ? 'gui.set.mem.fold' : 'gui.caps.configure')}
          </button>
        )}
        {row.needs ? (
          /* No switch: the tool is withheld for want of a key, and a toggle
             here would promise something the flip cannot deliver. The key is
             the switch -- fill it and the tool registers itself on the next
             start. */
          <span className="pnote">{t('gui.caps.needs_key')}</span>
        ) : (
          <button
            className="swi"
            role="switch"
            aria-checked={row.on}
            aria-label={t('gui.caps.toggle_aria', { name: row.name })}
            onClick={() => {
              /* Assigned on the source row, which is where the persistence
                 lives: live mode defines `on` as an accessor that writes
                 tools.disabledTools. Then a redraw, because the flip changed
                 state React does not hold. */
              row.on = !row.on
              store.redraw()
              toast(t(row.on ? 'gui.caps.enabled_x' : 'gui.caps.disabled_x', { name: row.name }))
            }}
          />
        )}
      </div>
    </div>
  )
}

function ToolCredRow({
  id,
  path,
  raw,
  say,
}: {
  id: string
  path: string
  raw: Record<string, unknown>
  say: () => void
}): JSX.Element {
  const box = useRef<HTMLInputElement>(null)
  const on = credOn(id, raw, path)
  const pick = WEB_VENDOR[id]
  const put = (v: string): void => {
    void store.write(path, v).then((r) => {
      if (r === 'notlive') say()
    })
  }
  /* Every path that currently holds the credential, not just the slot this
     editor writes: the tools resolve the slot first and fall back to the
     pre-vendor leaf, so emptying the slot alone leaves an upgraded config's key
     serving -- and emptying it after a replacement was pasted resurrects the
     older secret. */
  const clear = (): void => {
    const legacy = legacyCred(id, raw)
    void (async () => {
      const outcomes = [await store.write(path, '')]
      if (legacy && V(raw, legacy, '')) outcomes.push(await store.write(legacy, ''))
      if (outcomes.includes('notlive')) say()
    })()
  }
  return (
    <div className="tkrow tkey">
      {pick && (
        /* The vendor first, because it decides which slot the key beside it
           fills: switching vendors re-points the field, it never blanks a key. */
        <select
          className="mlend"
          aria-label={t('gui.caps.vendor')}
          value={webVendor(id, raw)}
          onChange={(e) => {
            void store.write(pick.path, e.currentTarget.value).then((r) => {
              if (r === 'notlive') say()
            })
          }}
        >
          {pick.vendors.map((v) => (
            <option key={v} value={v}>
              {WEB_VENDOR_LABEL[v] ?? v}
            </option>
          ))}
        </select>
      )}
      <span className={'kchip' + (on ? '' : ' off')}>
        <span className="led" />
        <span>{t(on ? 'gui.set.tls.key_set' : 'gui.set.tls.key_unset')}</span>
      </span>
      <KeyInput ref={box} placeholder="API Key" aria-label="API Key" />
      <button
        className="mini"
        onClick={() => {
          const v = (box.current?.value || '').trim()
          if (!v) {
            box.current?.focus()
            return
          }
          put(v)
        }}
      >
        {t(on ? 'gui.model.update' : 'gui.plug.connect')}
      </button>
      {on && (
        <button className="mini ghost" onClick={clear}>
          {t('gui.set.tls.clear')}
        </button>
      )}
    </div>
  )
}

/* One group of the inventory. The refusal lands on the card rather than the
   row, which is where the legacy nlSay put it -- a tool row has no `.crow`
   above it, so the search for a host walked up to the `.scard`. */
function ToolGroupCard({ g, rows, s }: { g: ToolGroup; rows: ToolRow[]; s: SettingsState }): JSX.Element {
  const [nl, say] = useNl()
  const on = rows.filter((r) => r.on).length
  return (
    <Scard title={t(g.label)} desc={t('gui.caps.tool_on', { on, all: rows.length })}>
      <div className="fset">
        {rows.map((r) => {
          const cred = toolCred(r.id, s.snap.raw)
          return (
            <Fragment key={r.id}>
              <ToolLine row={r} raw={s.snap.raw} s={s} />
              {r.id === 'image_generate' && s.toolKeyEdit === r.id && (
                <ImageModelPicker
                  model={String(V(s.snap.raw, 'tools.media.image.model', ''))}
                  quality={Vnull(s.snap.raw, 'tools.media.image.quality', undefined) as string | undefined}
                  say={say}
                />
              )}
              {cred && s.toolKeyEdit === r.id && <ToolCredRow id={r.id} path={cred} raw={s.snap.raw} say={say} />}
            </Fragment>
          )
        })}
      </div>
      {nl && <div className="nlmsg">{nl}</div>}
    </Scard>
  )
}

function ToolsetPage({ s }: { s: SettingsState }): JSX.Element {
  return (
    <>
      {s.snap.toolGroups.map((g) => {
        const rows = s.snap.tools.filter((r) => r.group === g.id)
        return rows.length ? <ToolGroupCard key={g.id} g={g} rows={rows} s={s} /> : null
      })}
    </>
  )
}

function PageBody({ tab, s }: { tab: string; s: SettingsState }): JSX.Element {
  switch (tab) {
    case 'look':
      return <LookPage />
    case 'notify':
      return <NotifyPage />
    case 'keys':
      return <KeysPage />
    case 'about':
      return <AboutPage />
    case 'model':
      return <ModelPage s={s} />
    case 'perm':
      return <PermPage s={s} />
    case 'memory':
      return <MemoryPage s={s} />
    case 'proact':
      return <ProactPage />
    case 'exec':
      return <ExecPage s={s} />
    case 'channel':
      return <ChannelPage s={s} />
    case 'data':
      return <DataPage s={s} />
    case 'toolset':
      return <ToolsetPage s={s} />
    default:
      return <UsagePage s={s} />
  }
}

function Panel({ s }: { s: SettingsState }): JSX.Element {
  const tab = SET_TITLE[s.tab] ? s.tab : 'usage'
  return (
    <div className="panel" data-on="true" style={tab === 'model' ? { animation: 'none' } : undefined}>
      <PageBody tab={tab} s={s} />
    </div>
  )
}

function Nav({ tab }: { tab: string }): JSX.Element {
  /* In the narrow chip-strip mode the active section can sit past the fold;
     bring it back into view whenever the dialog redraws. */
  useEffect(() => {
    const cur = document.querySelector<HTMLElement>('#snavList [aria-current="true"]')
    if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  })
  return (
    <>
      {SET_GROUPS.map((g) => (
        <Fragment key={g.key}>
          <div className="grp">{t(g.key)}</div>
          {g.pages.map(([id, key]) => (
            <button key={id} className="sitem" aria-current={id === tab} onClick={() => store.setTab(id)}>
              <svg viewBox="0 0 24 24" aria-hidden="true" dangerouslySetInnerHTML={{ __html: SET_ICO[id] ?? SET_ICO.about ?? '' }} />
              <span>{t(key)}</span>
            </button>
          ))}
        </Fragment>
      ))}
    </>
  )
}

/* Whichever drawer the store says is open, addressed by slug rather than by
   "the selected provider": it writes to the one whose button was pressed, and a
   rail click while it is open must not silently retarget that write. */
function Drawers({ s }: { s: SettingsState }): JSX.Element | null {
  const pv = s.drawer ? s.snap.providers.find((p) => p.id === s.drawer?.slug) : undefined
  if (!pv || !s.drawer) return null
  if (s.drawer.mode === 'add') return <AddModelDrawer key={pv.id} pv={pv} s={s} />
  return <CatalogueDrawer key={pv.id} pv={pv} s={s} />
}

export function SettingsApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.getState)
  /* The header is static markup the legacy drawSettings wrote into; the
     island keeps doing exactly that. */
  useEffect(() => {
    const title = document.getElementById('setTitle')
    if (title) title.textContent = t(SET_TITLE[s.tab] ?? 'gui.nav.set')
    const sub = document.getElementById('setSub')
    if (sub) {
      sub.textContent = ''
      ;(sub as HTMLElement).hidden = true
    }
  })
  const navHost = document.getElementById('snavList')
  return (
    <>
      {navHost ? createPortal(<Nav tab={s.tab} />, navHost) : null}
      <Panel key={`${s.tab}:${s.epoch}`} s={s} />
      {/* Outside the keyed panel on purpose. `epoch` remounts that subtree on
          every write so its uncontrolled fields restart from the freshly
          loaded values -- and a drawer in there was remounted by each row it
          added, replaying its slide-in and emptying its search box. Keyed by
          the provider alone, so it survives the writes it causes and resets
          only when it is aimed somewhere else. */}
      <Drawers s={s} />
    </>
  )
}
