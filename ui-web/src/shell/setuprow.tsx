/* The row shape the three set-up-once pages share: sub-agents, entrances,
 * schedules.
 *
 * One shape, so that reading one of those pages teaches the other two. A row
 * answers three questions and nothing else -- who it is, what state it is in,
 * and the one thing to do about it now -- and the whole row is a door to the
 * detail sheet where everything else lives. What used to differ per page (a
 * card here, a tile there, a paragraph of prose under one of them) was a
 * difference in drawing, never in meaning.
 *
 * Two rules this module exists to hold:
 *
 * - No prose. A row carries state facts ("ready, verified 3h ago", "2 of 6
 *   credentials"), never an explanation of what the thing is. Descriptions are
 *   editable content and belong in the sheet's own field; a page that prints
 *   them under every row was reading the model's prompt text out loud.
 * - One action. Whichever action the state actually calls for -- install,
 *   connect, test, run -- and never a second one beside it. Destructive verbs
 *   are not row verbs at all: they live in the sheet's overflow menu.
 */

import type { JSX, ReactNode } from 'react'

/* Eight tints, picked from the name so a row keeps its colour across reloads
   without anything having to store one. */
export function tileHue(name: string): number {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return h % 8
}

export function Tile({ name }: { name: string }): JSX.Element {
  return <span className={'pmtile th' + tileHue(name)}>{(name[0] || '?').toUpperCase()}</span>
}

/* The state dot. `ok` is the quiet default and carries no extra class, so a
   healthy list has no colour in it at all -- which is what makes the one amber
   dot in a list of twelve findable. */
export function Led({ cls }: { cls: string }): JSX.Element {
  return <span className={'led' + (cls === 'ok' ? '' : ' ' + cls)} />
}

export interface SetupRowProps {
  name: string
  /* The letter tile, or nothing. A channel and an agent are things with an
     identity worth a mark; a scheduled job is a sentence, and giving it an
     initial in a coloured square dressed a task up as an account. */
  tile?: boolean
  /* State: the dot's class, and the one line of fact beside the name. An empty
     `text` draws no line rather than an empty one -- "nothing measured" is not
     a status worth a row of its own height. */
  state: { cls: string; text: string }
  /* Short labels that qualify what this row is -- built-in, bundled, command
     line. Never a
     sentence, and never the same word the state line already says. */
  tags?: ReactNode
  /* The one thing to do now, or nothing. */
  act?: ReactNode
  onOpen: () => void
  /* Marks the row whose sheet stands open, so the list keeps saying which one
     you are looking at. */
  sel?: boolean
  /* A right-hand column of settled fact, before the action -- a schedule's
     "every weekday 09:30 / next tomorrow 09:30". */
  extra?: ReactNode
  /* A third line under the facts. Clicks inside it do not open the row. */
  foot?: ReactNode
}

export function SetupRow({ name, state, tags, act, onOpen, sel, tile = true, extra, foot }: SetupRowProps): JSX.Element {
  return (
    <div
      className={'surow' + (tile ? '' : ' notile') + (state.cls === 'bad' ? ' bad' : '')}
      role="button"
      tabIndex={0}
      aria-current={sel ? 'true' : undefined}
      onClick={onOpen}
      onKeyDown={(e) => {
        /* The row's own keys only. A row is a focusable button holding the real
           buttons, and a key event on one of those bubbles to here: the
           preventDefault below then cancelled that button's own activation, so
           every row action was unreachable from the keyboard while the mouse
           worked. `.suact` stops the click for the same reason; a click does not
           reach here, a keydown does. */
        if (e.target !== e.currentTarget) return
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen()
        }
      }}
    >
      {tile ? <Tile name={name} /> : null}
      <div className="nm">
        <Led cls={state.cls} />
        <b>{name}</b>
        {tags}
      </div>
      {/* The action stops the click here: opening the sheet as a side effect of
          pressing Install is how a button press ends up looking like it did
          two things. */}
      <div className="suact" onClick={(e) => e.stopPropagation()}>
        {act}
      </div>
      {state.text ? (
        <div className={'sufacts' + (state.cls === 'ok' || state.cls === 'off' ? '' : ' ' + state.cls)}>
          {state.text}
        </div>
      ) : null}
      {extra ? <div className="suextra">{extra}</div> : null}
      {/* A third line, for the one kind of row that has a history: the last
          thing that happened, which is its own link. */}
      {foot ? (
        <div className="sufoot2" onClick={(e) => e.stopPropagation()}>
          {foot}
        </div>
      ) : null}
    </div>
  )
}

/* A group heading: name, count, and -- for a group that has one -- a single
   tool on the right. No hint sentence under it; if a group needs a sentence to
   explain what it holds, its name is wrong. */
export function SetupGroup({
  label,
  count,
  children,
}: {
  label: string
  count: number
  children: ReactNode
}): JSX.Element {
  return (
    <div className="sugrp">
      <div className="hd">
        <b>{label}</b>
        <span className="n">{String(count)}</span>
      </div>
      {children}
    </div>
  )
}
