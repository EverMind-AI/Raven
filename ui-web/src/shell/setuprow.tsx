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

import { useState } from 'react'

import { t } from './bridge'

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
  /* The row's mark. Unset draws the letter tile; `false` draws none -- a
     scheduled job is a sentence, and giving it an initial in a coloured square
     dressed a task up as an account. A node is for a row whose identity is a
     thing in itself rather than its name: an agent draws its brand, which is a
     fact about the package behind it and not about what the reader called it.
     Same shape as `SheetHead`'s `tile`, so a row and the sheet it opens can
     take the one mark. */
  tile?: ReactNode
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

export function SetupRow({ name, state, tags, act, onOpen, sel, tile, extra, foot }: SetupRowProps): JSX.Element {
  /* Resolved once, and against `undefined` rather than for truthiness: "no
     tile" and "the default tile" are different answers that a `tile ? ...`
     test reads as the same one. */
  const mark = tile === undefined ? <Tile name={name} /> : tile
  return (
    <div
      className={'surow' + (mark ? '' : ' notile') + (state.cls === 'bad' ? ' bad' : '')}
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
      {mark}
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
   explain what it holds, its name is wrong.

   `folds` opens that right-hand slot for the one tool a group can carry: its
   own visibility. A group asks for it when its rows are a catalogue rather
   than an inventory -- everything the product could talk to, most of which
   this machine has never had -- so the reader scrolling for the four agents
   they actually run is scrolling past nine they do not. It starts shut, since
   a group that had to ask for the switch is a group whose length is the
   problem.

   Absent, and the heading is what it always was: no button, no state, nothing
   for the two pages that never asked to change. */
export function SetupGroup({
  label,
  count,
  folds,
  children,
}: {
  label: string
  count: number
  folds?: boolean
  children: ReactNode
}): JSX.Element {
  const [shut, setShut] = useState(true)
  /* Only a folding group reads it. The hook still runs -- it is a hook -- but
     a group that never folds cannot be shut, and reading the state anyway is
     how one would end up hidden by a prop nobody passed. */
  const hidden = !!folds && shut
  return (
    <div className="sugrp">
      <div className="hd">
        <b>{label}</b>
        <span className="n">{String(count)}</span>
        {folds ? (
          <button aria-expanded={!hidden} className="gfold" onClick={() => setShut(!shut)} type="button">
            {t(hidden ? 'gui.grp.show' : 'gui.grp.hide')}
          </button>
        ) : null}
      </div>
      {hidden ? null : children}
    </div>
  )
}
