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
  /* The left column: a search row, then a list. */
  side: ReactNode
  /* The right column: the picked row, or a line saying to pick one. */
  children: ReactNode
}): JSX.Element {
  return (
    <div className="two-pane">
      <div className="two-pane-side">{side}</div>
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

/** A labelled block under the header. */
export function TwoPaneSection({ label, act, children }: {
  label?: ReactNode
  act?: ReactNode
  children: ReactNode
}): JSX.Element {
  return (
    <div className="two-pane-sec">
      {label || act ? <div className="two-pane-lab">{label}<span className="two-pane-sp" />{act}</div> : null}
      {children}
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

/** Nothing picked, or nothing to pick. */
export function TwoPaneNone({ children }: { children: ReactNode }): JSX.Element {
  return <div className="two-pane-none">{children}</div>
}
