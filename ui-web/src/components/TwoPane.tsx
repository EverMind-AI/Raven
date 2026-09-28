/* The two-pane frame a settings section is drawn in: a list on the left, and
 * what the picked row is on the right.
 *
 * Three domains draw one -- channels, schedules and memory -- and each used to
 * be a module page of its own, where the list ran the width of the window and
 * the detail was a place you navigated TO: a modal over the list, a second
 * screen behind a back button, a drawer sliding in from the right. Inside the
 * settings dialog none of those fit: the dialog is already a layer, and a
 * second one over it is a layer over a layer. So the detail stands beside the
 * list instead, which is also what makes the two readable at once -- picking
 * another channel is a click rather than a close and a reopen.
 *
 * Here rather than in any of the three because all three draw the same frame,
 * and a class name is shared state: these are `two-pane*`, this file owns them,
 * and the rules are in src/styles/page.css beside the dialog they sit in
 * (scripts/check-class-namespace.mjs reads the ownership off this file's name).
 * What a row HOLDS is still the domain's -- a channel has a logo and a switch,
 * a schedule has a next-run time, a memory has a date -- so the row below takes
 * those as children and decides only the shape.
 */
import { t } from '../i18n/t'

import type { JSX, ReactNode } from 'react'

export function TwoPane({ side, children }: {
  /* The left column: a search row, then a list. Absent when there is nothing
     for a list to hold -- no rows at all, or no way to read them -- and the
     frame is then one column: an empty search box over an empty list beside
     the state that explains them was two halves saying one thing, lopsided. */
  side?: ReactNode
  /* The right column: the picked row, or a line saying to pick one. */
  children: ReactNode
}): JSX.Element {
  return (
    <div className={side === undefined ? 'two-pane two-pane-solo' : 'two-pane'}>
      {side === undefined ? null : <div className="two-pane-side">{side}</div>}
      <div className="two-pane-main">{children}</div>
    </div>
  )
}

/* The search field, and the one button beside it that adds a row. The field is
   controlled: the lists it filters are short enough that a keystroke redrawing
   them costs nothing, and an uncontrolled one would need a ref to be cleared. */
export function TwoPaneFind({ value, onChange, placeholder, onAdd, addLabel }: {
  value: string
  onChange(v: string): void
  placeholder: string
  /* Absent where a section cannot be added to from here. */
  onAdd?(): void
  addLabel?: string
}): JSX.Element {
  return (
    <div className="two-pane-find">
      <div className="two-pane-search">
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" /></svg>
        <input
          value={value}
          placeholder={placeholder}
          aria-label={placeholder}
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => onChange(e.target.value)}
        />
      </div>
      {onAdd ? (
        <button type="button" className="two-pane-add" aria-label={addLabel ?? t('gui.add')} onClick={onAdd}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
        </button>
      ) : null}
    </div>
  )
}

/** The scrolling column of rows. */
export function TwoPaneList({ children }: { children: ReactNode }): JSX.Element {
  return <div className="two-pane-list">{children}</div>
}

/* The list before its rows are in.
 *
 * All three sections drawn in this frame answered the wait with nothing at
 * all -- `!loaded && !rows.length ? null` in channels and schedules, a line of
 * grey text in memory -- so opening one of them showed an empty column beside
 * an empty pane, which is what "there are no channels" looks like. The rows
 * this becomes are two lines and a trailing control, so that is what waits
 * here, at the row's own height.
 *
 * Here rather than in each of the three: the frame owns `two-pane*` (see this
 * file's header), and a domain drawing its own bars would be a fourth name for
 * one shape.
 */
export function TwoPaneWait({ rows = 7 }: { rows?: number }): JSX.Element {
  return (
    <div className="two-pane-wait" role="status" aria-busy="true" aria-label={t('gui.settings.loading')}>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="two-pane-row">
          <span className="two-pane-hit">
            <span className="two-pane-txt">
              <span className="two-pane-wbar" style={{ width: `${46 + ((i * 23) % 38)}%`, height: '11px' }} />
              <span className="two-pane-wbar" style={{ width: `${34 + ((i * 17) % 30)}%`, height: '9px' }} />
            </span>
          </span>
        </div>
      ))}
    </div>
  )
}

/** A heading between two runs of rows (on and off, a memory's kind). */
export function TwoPaneGroup({ children }: { children: ReactNode }): JSX.Element {
  return <div className="two-pane-grp">{children}</div>
}

/* One row. `current` is what the right column is showing, `off` is a row whose
   subject is turned off -- the name greys, which is the whole of the
   difference, because a row you cannot read is not a row you can turn back
   on. */
export function TwoPaneRow({ current, off, icon, name, sub, tone, when, trailing, onOpen }: {
  current: boolean
  off?: boolean
  /* A logo or a state dot, drawn before the name. */
  icon?: ReactNode
  name: string
  /* The second line: what it does, or what state it is in. */
  sub?: string
  /* Which reading the second line takes. */
  tone?: 'warn' | 'live' | 'bad'
  /* A stamp on the right of the row, in the mono face. */
  when?: string
  /* A control the row carries, outside the button so it is its own hit. */
  trailing?: ReactNode
  onOpen(): void
}): JSX.Element {
  return (
    <div className={off ? 'two-pane-row two-pane-off' : 'two-pane-row'} aria-current={current}>
      <button type="button" className="two-pane-hit" onClick={onOpen}>
        {icon}
        <span className="two-pane-txt">
          <span className="nm" title={name}>{name}</span>
          {sub ? <span className={tone ? `ds ${tone}` : 'ds'}>{sub}</span> : null}
        </span>
        {when ? <span className="two-pane-when">{when}</span> : null}
      </button>
      {trailing}
    </div>
  )
}

/* The right column's own header: what is picked, what it is, and whatever
   control belongs to the whole of it. */
export function TwoPaneHead({ icon, name, meta, aside }: {
  icon?: ReactNode
  name: string
  meta?: ReactNode
  aside?: ReactNode
}): JSX.Element {
  return (
    <div className="two-pane-head">
      {icon}
      <div className="ttl">
        <div className="nm">{name}</div>
        {meta ? <div className="two-pane-meta">{meta}</div> : null}
      </div>
      {aside}
    </div>
  )
}

/* A labelled block under the header. `tight` is the spacing a run of them
   takes when the label names one box rather than a section: a form of six
   credentials at the section's own rhythm reads as six sections. */
export function TwoPaneSection({ label, act, tight, children }: {
  label?: ReactNode
  act?: ReactNode
  tight?: boolean
  children: ReactNode
}): JSX.Element {
  return (
    <div className={tight ? 'two-pane-sec two-pane-tight' : 'two-pane-sec'}>
      {label || act ? <div className="two-pane-lab">{label}<span className="two-pane-sp" />{act}</div> : null}
      {children}
    </div>
  )
}

/** Controls side by side, inside a section or a foot. */
export function TwoPaneIn({ children }: { children: ReactNode }): JSX.Element {
  return <div className="two-pane-in">{children}</div>
}

/* The controls that belong to the whole of what is picked rather than to one
   section of it, at the foot of the column over a hairline -- which is where
   "delete this" belongs, far from the boxes a reader is filling in. */
export function TwoPaneFoot({ children }: { children: ReactNode }): JSX.Element {
  return (
    <div className="two-pane-foot">
      <div className="two-pane-in">{children}</div>
    </div>
  )
}

/* One labelled box. The human sentence is the label; the raw config key rides
   beside it in the mono face rather than in a tooltip, because a reader
   checking a field against a doc needs to see the key without hovering. */
export function TwoPaneField({ label, keyName, tag, children }: {
  label: string
  /* The config key, when it says something the label does not. */
  keyName?: string
  /* One word on what is already true of this field -- that it is set. */
  tag?: string
  children: ReactNode
}): JSX.Element {
  return (
    <div className="two-pane-sec two-pane-tight">
      <div className="two-pane-lab">
        {label}
        {keyName ? <span className="two-pane-sub">{keyName}</span> : null}
        {tag ? <span className="two-pane-tag">{tag}</span> : null}
      </div>
      <div className="two-pane-in">
        <span className="two-pane-box">{children}</span>
      </div>
    </div>
  )
}

/* The switch a row and a header both carry. Here rather than borrowed from the
   page's own `.swi`, so that a class name has one writer: the rules are the
   same ones, and page.css says both names on the same line. */
export function TwoPaneSwitch({ on, label, disabled, onChange }: {
  on: boolean
  label: string
  disabled?: boolean
  onChange(): void
}): JSX.Element {
  return (
    <button
      type="button"
      className="two-pane-swi"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={onChange}
    />
  )
}

/* The right column with nothing in it: either nothing is picked yet, or there
   is nothing to pick. Both used to be one grey sentence at the top of an empty
   column -- the second one a paragraph -- which read as a leftover rather than
   as a state. So it is a mark, a line saying what the state is, a line saying
   how it ends, and the one control that ends it, centred in the column. */
export function TwoPaneNone({ icon, title, action, detail, children }: {
  /* The section's own mark; the picking glyph when absent. */
  icon?: ReactNode
  /* What the state is. Absent on "pick one", where the line below is all of it. */
  title?: string
  action?: { label: string; onClick(): void }
  /* The machine's own words for a failure, under the sentence a reader acts on. */
  detail?: string
  /* How the state ends. */
  children?: ReactNode
}): JSX.Element {
  return (
    <div className={title ? 'two-pane-none' : 'two-pane-none two-pane-quiet'}>
      <span className="two-pane-glyph" aria-hidden="true">{icon ?? PICK_GLYPH}</span>
      {title ? <div className="two-pane-nt">{title}</div> : null}
      {children ? <div className="two-pane-ns">{children}</div> : null}
      {detail ? <div className="two-pane-nd">{detail}</div> : null}
      {action ? (
        <button type="button" className="mini go" onClick={action.onClick}>{action.label}</button>
      ) : null}
    </div>
  )
}

const PICK_GLYPH = (
  <svg viewBox="0 0 24 24"><rect x="3.5" y="4.5" width="17" height="15" rx="2.5" /><path d="M9.5 4.5v15M6 8.5h1M6 11.5h1" /></svg>
)

/* The list with no row matching the search. The rows' own column, so a mark
   and a line where the first row would be rather than a paragraph. */
export function TwoPaneNoHit({ children }: { children: ReactNode }): JSX.Element {
  return (
    <div className="two-pane-nohit">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4M8.5 11h5" /></svg>
      <span>{children}</span>
    </div>
  )
}
