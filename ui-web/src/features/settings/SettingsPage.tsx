import { Fragment, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { shell, t } from '../../shell/bridge'
import * as lookStore from '../../shell/look'
import * as notifications from '../../shell/notifications'
import { open as openUrl } from '../../shell/open-url'
import { isMac, modKey } from '../../shell/platform'
import { hint as reachHint, text as reachText } from '../../shell/reach'
import { show as toast } from '../../shell/toast'
import { open as openConn } from '../connections/store'
import { count as sessionCount, deleteAll as deleteAllSessions } from '../rail/store'
import * as store from './store'
import { ImageModelPicker } from './ImageModelPicker'

import type { SettingsState } from './store'
import type { EverosSection, ProviderRow, ToolGroup, ToolRow } from './types'
import type { JSX, ReactNode } from 'react'

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
const kindLabel = (kind?: string): string => t('gui.model.kind.' + (kind || 'key'), undefined, t('gui.model.kind.key'))
const loginCmd = (slug: string): string => `raven provider login ${String(slug).replace(/_/g, '-')}`
const OFF_HEAD = 5

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

function StatTiles({ rows }: { rows: Array<[string | null, string]> }): JSX.Element {
  return (
    <div className="stats">
      {rows.map(([v, k], i) => (
        <div className="stat" key={i}>
          <div className={'v' + (v == null ? ' none' : '')}>{v == null ? t('gui.set.nodata') : String(v)}</div>
          <div className="k">{k}</div>
        </div>
      ))}
    </div>
  )
}

/* rows: [label, value, kind]. kind '' | 'unset' | 'ok' */
function KvList({ rows }: { rows: Array<[string, string, string?]> }): JSX.Element {
  return (
    <div className="skv">
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
      <Scard title={`${t('gui.set.usg.llm')} · ${t('gui.set.usg.window', { d: u.days })}`}>
        <StatTiles
          rows={[
            [String(u.llm.total.calls), t('gui.set.usg.calls')],
            [fmtTok(u.llm.total.input_tokens), t('gui.set.usg.in')],
            [fmtTok(u.llm.total.output_tokens), t('gui.set.usg.out')],
            ['$' + u.llm.total.cost_usd.toFixed(2), t('gui.set.usg.cost')],
          ]}
        />
        {u.llm.models.length > 0 && (
          <KvList
            rows={u.llm.models
              .slice(0, 12)
              .map((m) => [m.model, `${m.calls} × · ${fmtTok(m.input_tokens + m.output_tokens)} tok · $${m.cost_usd.toFixed(2)}`])}
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

function ModelChips({ pv }: { pv: ProviderRow }): JSX.Element {
  return (
    <div>
      <div className="pnote">{t('gui.model.count_pick', { n: pv.models.length })}</div>
      {pv.models.length > 0 && (
        <div className="chips">
          {pv.models.slice(0, 10).map((m) => (
            <span className="chipm" key={m}>
              <span>{shortModel(m)}</span>
              <button
                title={t('gui.model.remove')}
                aria-label={`${t('gui.model.remove')} ${m}`}
                onClick={() => void store.providerRun('remove_model', { slug: pv.id, model: m })}
              >
                ✕
              </button>
            </span>
          ))}
          {pv.models.length > 10 && <span className="pnote">{t('gui.model.count_more', { n: pv.models.length - 10 })}</span>}
        </div>
      )}
    </div>
  )
}

function ProvForm({ pv, s }: { pv: ProviderRow; s: SettingsState }): JSX.Element {
  const base = useRef<HTMLInputElement>(null)
  const key = useRef<HTMLInputElement>(null)
  const add = useRef<HTMLInputElement>(null)
  const [copied, setCopied] = useState(false)
  const save = (): void => {
    const params: Record<string, unknown> = { slug: pv.id }
    if (pv.kind !== 'local') params.api_key = key.current?.value.trim() ?? ''
    if (base.current?.value.trim()) params.api_base = base.current.value.trim()
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
  return (
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
          {(pv.needsBase || pv.kind === 'endpoint') && (
            <div className="keyrow">
              <input ref={base} type="text" placeholder={t(pv.kind === 'local' ? 'gui.model.base_ph_local' : 'gui.model.base_ph')} />
            </div>
          )}
          <div className="keyrow">
            {pv.kind !== 'local' && (
              <input
                ref={key}
                type="password"
                placeholder={pv.on ? t('gui.model.key_ph_update') : t('gui.model.key_ph', { name: pv.name })}
                aria-label={`${pv.name} API Key`}
              />
            )}
            <button className="mini" onClick={save}>
              {t(pv.on ? 'gui.model.update' : 'gui.model.connect')}
            </button>
          </div>
          {pv.env && <div className="pnote">{t('gui.model.env_hint', { env: pv.env })}</div>}
        </>
      )}
      <div className="keyrow">
        <input ref={add} type="text" placeholder={t('gui.model.add_ph')} />
        <button
          className="mini ghost"
          onClick={() => {
            const v = add.current?.value.trim()
            if (!v) return
            void store.providerRun('add_model', { slug: pv.id, model: v })
          }}
        >
          {t('gui.add')}
        </button>
      </div>
      <ModelChips pv={pv} />
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
  )
}

function ProvCard({ pv, s }: { pv: ProviderRow; s: SettingsState }): JSX.Element {
  const open = s.provOpen === pv.id
  const bits = [pv.on ? t('gui.model.state.connected') : kindLabel(pv.kind)]
  if (pv.models.length) bits.push(t('gui.model.count', { n: pv.models.length }))
  if (!pv.on && pv.kind === 'oauth') bits.push(t('gui.model.needs_login'))
  return (
    <div className={'pcard' + (open ? ' open' : '')}>
      <div className="nm">
        <span className={'led' + (pv.on ? '' : ' warn')} />
        <span>{pv.name}</span>
        {pv.id === s.snap.curProvider && <span className="tagm">{t('gui.model.is_default')}</span>}
      </div>
      <div className="mo">{bits.join(' · ')}</div>
      <div className="ctl">
        <button className={'mini' + (pv.on || open ? ' ghost' : '')} aria-expanded={open} onClick={() => store.provToggle(pv.id)}>
          {open ? t('gui.model.collapse') : pv.on ? t('gui.model.manage') : t('gui.model.connect')}
        </button>
      </div>
      {open && <ProvForm pv={pv} s={s} />}
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
  const on = s.snap.providers.filter((x) => x.on)
  const off = s.snap.providers.filter((x) => !x.on)
  /* Expanding a card lands the caret in its first field, as the legacy
     panel did after its redraw. */
  useEffect(() => {
    if (!s.provFocus) return
    store.clearProvFocus()
    const card = document.querySelector('#spanels .pcard.open')
    if (!card) return
    const field = card.querySelector<HTMLInputElement>('.pform input')
    if (field) field.focus()
    if (card.scrollIntoView) card.scrollIntoView({ block: 'nearest' })
  })
  return (
    <>
      <div className="scard">
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
      {/* Connected first and in its own card: which providers are live is the
          one thing this panel is asked. */}
      <Scard title={t('gui.model.connected', { n: on.length })}>
        {!on.length ? (
          <div className="pnote">{t('gui.model.none_connected')}</div>
        ) : (
          <div className="fset">
            {on.map((pv) => (
              <ProvCard key={pv.id} pv={pv} s={s} />
            ))}
          </div>
        )}
      </Scard>
      <Scard title={t('gui.model.others', { n: off.length })}>
        <div className="fset">
          {(s.provAll ? off : off.slice(0, OFF_HEAD)).map((pv) => (
            <ProvCard key={pv.id} pv={pv} s={s} />
          ))}
        </div>
        {off.length > OFF_HEAD && (
          <button className="mini ghost" aria-expanded={s.provAll} onClick={() => store.provAllToggle()}>
            {s.provAll ? t('gui.model.collapse') : t('gui.model.expand_rest', { n: off.length - OFF_HEAD })}
          </button>
        )}
      </Scard>
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
    <Scard title={t('gui.set.prm.guard')}>
      <KvList
        rows={[
          [t('gui.set.prm.workspace'), onoff(V(raw, 'tools.restrictToWorkspace', false) === true)],
          [t('gui.set.prm.sandbox'), sbName, sb === 'none' ? 'unset' : 'ok'],
        ]}
      />
      <SwiRow
        label={t('gui.set.prm.destructive')}
        hint={t('gui.set.prm.destructive_w')}
        k="tools.exec.allowDestructiveCommands"
        on={V(raw, 'tools.exec.allowDestructiveCommands', false) === true}
        confirm={(commit) =>
          sh.confirmAsk(
            t('gui.set.prm.destructive_confirm'),
            t('gui.set.prm.destructive_body'),
            t('gui.set.prm.destructive_yes'),
            commit,
          )
        }
      />
      <Srmk configPath={s.snap.configPath} />
    </Scard>
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
              <input ref={key} type="password" autoComplete="off" placeholder={cur.api_key_set ? t('gui.set.tls.key_set') : 'API Key'} />
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
      <input ref={box} type="password" placeholder="API Key" autoComplete="off" />
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
    </>
  )
}
