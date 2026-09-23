/* The settings dialog's form primitives: card, row, segmented pick, switch,
   stepper, chip, breadcrumb, expandable row. Markup and class names are the
   prototype's, under the domain's prefix, so the front end can restyle the
   dialog from styles.css alone. */
import { t } from '../../i18n/t'

import type { JSX, ReactNode } from 'react'

export function Card({ title, act, raw, children }: {
  title?: string
  act?: ReactNode
  /* Children placed directly in the card rather than in a `.settings-rows`
     list: tables, bars, the vendor list sheet. */
  raw?: boolean
  children?: ReactNode
}): JSX.Element {
  return (
    <div className="settings-card">
      {(title || act) && (
        <div className={act ? 'settings-ch settings-hasact' : 'settings-ch'}>
          <div className="settings-t">{title}</div>
          {act}
        </div>
      )}
      {raw ? children : <div className="settings-rows">{children}</div>}
    </div>
  )
}

/* A labelled block with no card around it, which is the prototype's own shape
   for a detail pane: the label IS the separation. A border per block turns a
   pane of three facts into three boxes, and the boxes then need their own
   inner padding, their own heading size and their own gap -- three decisions
   the label had already made. `act` is the one control a heading carries (add
   a model), `tight` the spacing a run of one-box labels takes. */
export function Sec({ label, sub, act, tight, children }: {
  label?: ReactNode
  sub?: string
  act?: ReactNode
  tight?: boolean
  children?: ReactNode
}): JSX.Element {
  return (
    <div className={tight ? 'settings-sec settings-sec-tight' : 'settings-sec'}>
      {(label || act) && (
        <div className="settings-lab">
          {label}
          {sub && <span className="settings-sub2">{sub}</span>}
          {act && <><span className="settings-sp" />{act}</>}
        </div>
      )}
      {children}
    </div>
  )
}

/* A fold over the half of a pane few people touch. Closed is the point: the
   heading says what is behind it and costs one line, where an always-open
   card of overrides is the largest thing on a page about one key. */
export function Fold({ open, label, onToggle, children }: {
  open: boolean
  label: string
  onToggle(): void
  children: ReactNode
}): JSX.Element {
  return (
    <div className="settings-sec">
      <button type="button" className="foldcap" aria-expanded={open} onClick={onToggle}>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg>
        {label}
      </button>
      {open ? children : null}
    </div>
  )
}

export function Row({ label, sub, stack, k, children }: {
  label?: ReactNode
  sub?: string
  /* Label above the control rather than beside it: key fields, lists. */
  stack?: boolean
  /* A whole label cell of the caller's own (the role rows). */
  k?: ReactNode
  children?: ReactNode
}): JSX.Element {
  return (
    <div className={stack ? 'settings-row settings-stack' : 'settings-row'}>
      {k ?? (
        <div className="settings-k">
          {label}
          {sub && <span className="settings-sub2">{sub}</span>}
        </div>
      )}
      <div className="settings-ctl">{children}</div>
    </div>
  )
}

export function Seg({ opts, value, onPick, disabled }: {
  opts: Array<[string, string]>
  value: string
  onPick(v: string): void
  disabled?: boolean
}): JSX.Element {
  return (
    <span className="settings-seg">
      {opts.map(([v, label]) => (
        <button key={v} type="button" aria-pressed={v === value} disabled={disabled} onClick={() => onPick(v)}>
          {label}
        </button>
      ))}
    </span>
  )
}

export function Switch({ on, onChange, label, disabled }: {
  on: boolean
  onChange(v: boolean): void
  label: string
  disabled?: boolean
}): JSX.Element {
  return (
    <button
      type="button"
      className="settings-swi"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!on)}
    />
  )
}

export function Stepper({ value, unit, onBump }: {
  value: number
  unit?: string
  onBump(delta: number): void
}): JSX.Element {
  return (
    <span className="settings-stepper">
      <button type="button" aria-label={t('gui.settings.less')} onClick={() => onBump(-1)}>{'−'}</button>
      <span className="settings-v">{value}{unit ? ` ${unit}` : ''}</span>
      <button type="button" aria-label={t('gui.settings.more')} onClick={() => onBump(1)}>+</button>
    </span>
  )
}

/* A status chip. `act` makes it a button (the failed chip's retry). */
export function Chip({ state, children, onClick }: {
  state: 'on' | 'off' | 'warn'
  children: ReactNode
  onClick?(): void
}): JSX.Element {
  const cls = `settings-chip settings-${state}`
  if (onClick) {
    return (
      <button type="button" className={`${cls} settings-act`} onClick={onClick}>
        <span className="settings-led" />{children}
      </button>
    )
  }
  return <span className={cls}><span className="settings-led" />{children}</span>
}

export function Spin({ children }: { children?: ReactNode }): JSX.Element {
  return <span className="settings-rov"><span className="settings-spin" />{children}</span>
}

/* The hint text a row answers with when it has nothing to edit. */
export function Rov({ children, warn }: { children: ReactNode; warn?: boolean }): JSX.Element {
  return <span className="settings-rov" style={warn ? { color: 'var(--clay)' } : undefined}>{children}</span>
}

export function Tag({ children }: { children: ReactNode }): JSX.Element {
  return <span className="settings-kd">{children}</span>
}

/* The detail pages' first line: back, the name, its tags, and what the caller
   puts on the right. */
export function Crumb({ back, onBack, name, mono, children }: {
  back: string
  onBack(): void
  name: string
  mono?: boolean
  children?: ReactNode
}): JSX.Element {
  return (
    <div className="settings-crumb">
      <button type="button" className="mini ghost" onClick={onBack}>{'←'} {back}</button>
      <span className={mono ? 'settings-nm mono' : 'settings-nm'}>{name}</span>
      {children}
    </div>
  )
}

export function Grow(): JSX.Element {
  return <span style={{ flex: 1 }} />
}

const OPEN_PATH = (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M14 4h6v6" /><path d="M20 4 11.5 12.5" />
    <path d="M18 14v4.5A1.5 1.5 0 0 1 16.5 20h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10" />
  </svg>
)

/* The "get a key" link beside a key field, when the vendor has a page. */
export function KeyLink({ url }: { url?: string | null }): JSX.Element | null {
  if (!url) return null
  const label = t('gui.settings.get_key')
  return (
    <a className="settings-iconbtn settings-lnkico" href={url} target="_blank" rel="noopener" title={label} aria-label={label}>
      {OPEN_PATH}
    </a>
  )
}

/* An action drawn as an icon: open a file, remove a header. */
export function IconBtn({ label, onClick, glyph }: {
  label: string
  onClick(): void
  glyph?: 'open' | 'x'
}): JSX.Element {
  return (
    <button type="button" className="settings-iconbtn" title={label} aria-label={label} onClick={onClick}>
      {glyph === 'x' ? '✕' : OPEN_PATH}
    </button>
  )
}

/* A row that opens its own panel: the tool and plugin rows. The name is the
   button; the switch and the chip stay outside it. */
export function Xrow({ name, status, ctl, panel, open, dim, onToggle }: {
  name: ReactNode
  status?: ReactNode
  ctl: ReactNode
  panel?: ReactNode
  open: boolean
  dim?: boolean
  onToggle(): void
}): JSX.Element {
  const cls = ['settings-xrow', dim ? 'settings-dim' : '', open ? 'settings-open' : ''].filter(Boolean).join(' ')
  return (
    <>
      <div className={cls}>
        {panel ? (
          <button type="button" className="settings-xname" aria-expanded={open} onClick={onToggle}>
            <span className="settings-xn">{name}</span>
            <svg className="settings-chev" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg>
          </button>
        ) : (
          <span className="settings-xname"><span className="settings-xn">{name}</span></span>
        )}
        <div className="settings-ctl"><span className="settings-taglist">{status}{ctl}</span></div>
      </div>
      {open && panel && <div className="settings-cfg">{panel}</div>}
    </>
  )
}

export function InlineErr({ text }: { text: string }): JSX.Element | null {
  return text ? <div className="settings-inline-err" role="alert">{text}</div> : null
}

export function Search({ id, value, placeholder, onChange }: {
  id: string
  value: string
  placeholder: string
  onChange(v: string): void
}): JSX.Element {
  return (
    <div className="settings-srch">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" /></svg>
      <input id={id} value={value} placeholder={placeholder} autoComplete="off" spellCheck={false}
        onChange={(e) => onChange(e.currentTarget.value)} />
    </div>
  )
}

/* A path with a copy action, for the About page. */
export function PathVal({ path, onCopy }: { path: string; onCopy(): void }): JSX.Element {
  return (
    <span className="settings-pathv">
      <span className="settings-pp">{path}</span>
      <IconBtn label={t('gui.settings.copy_path')} onClick={onCopy} />
    </span>
  )
}

/* Compact counts: 1.2M, 34k. */
export function fmt(n: number): string {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n)
}
