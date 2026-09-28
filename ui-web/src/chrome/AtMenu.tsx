/* The "@" in the composer bar, and the menu it opens.
 *
 * Two things a conversation can be pointed at, behind one button: the folder
 * its turns run in, and the knowledge bases they may search. Both are answers
 * to the same question -- what is this about -- which is why they share a
 * button rather than each having one, and why the bar reads as a line of
 * actions rather than a row of chips.
 *
 * The folder row hands off to the workdir popover, which already owns picking
 * one; nothing here reimplements it. The bases row opens its own panel in
 * place, because ticking several is not a pick-one-and-close.
 *
 * The button and the menu share one anchor (`.chrome-anch`), the way the "+"
 * and the chips beside it do, so the stylesheet hangs the menu off the
 * button's top edge and nothing here measures an element.
 */

import { useSyncExternalStore } from 'react'

import { t } from '../i18n/t'
import * as lang from '../state/lang'
import * as mentions from '../state/mentions'
import * as wd from '../state/workdir'
import { FOLDER } from './WorkdirChip'

import type { JSX } from 'react'

function Glyph({ d }: { d: string }): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true" className="pico">
      <path d={d} />
    </svg>
  )
}

/** A stack of sheets: the drawing the knowledge page uses for a base. */
const BOOKS = 'M4 6.5A2 2 0 0 1 6 4.5h5v15H6a2 2 0 0 0-2 2ZM20 6.5a2 2 0 0 0-2-2h-5v15h5a2 2 0 0 1 2 2Z'

export function AtBtn(): JSX.Element {
  const s = useSyncExternalStore(mentions.subscribe, mentions.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const word = lang.attr('gui.at.point')
  return (
    <button
      className={s.picked.length ? 'plus chrome-at-set' : 'plus'}
      id="atBtn"
      aria-expanded={s.open ? 'true' : 'false'}
      aria-haspopup="true"
      data-tip={word}
      aria-label={word}
      onClick={() => mentions.toggle()}
    >
      <span aria-hidden="true">@</span>
      {/* How many bases it is pointed at, on the button rather than only
          inside the menu: the scope of a question is worth seeing while it is
          being typed, not only while it is being set. */}
      {s.picked.length > 0 && <span className="chrome-at-n">{s.picked.length}</span>}
    </button>
  )
}

function Bases({ s }: { s: mentions.MentionState }): JSX.Element {
  if (s.failed) {
    return (
      <div className="chrome-at-note">{s.failed}</div>
    )
  }
  if (s.bases === null) {
    return <div className="chrome-at-note">{t('gui.at.loading')}</div>
  }
  if (!s.bases.length) {
    return <div className="chrome-at-note">{t('gui.at.no_bases')}</div>
  }
  return (
    <>
      {s.bases.map((base) => {
        const on = s.picked.includes(base.id)
        return (
          <button
            key={base.id}
            className="prow chrome-at-row"
            role="menuitemcheckbox"
            aria-checked={on}
            onClick={() => mentions.pick(base.id)}
          >
            <span className="chrome-at-tick" aria-hidden="true">
              {on ? '✓' : ''}
            </span>
            <span className="nm">{base.name}</span>
            <span className="chrome-at-count">{t('gui.at.documents', { n: base.documents })}</span>
          </button>
        )
      })}
    </>
  )
}

export function AtPopover(): JSX.Element {
  const s = useSyncExternalStore(mentions.subscribe, mentions.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <div
      className="pop"
      id="atPop"
      data-open={s.open ? 'true' : 'false'}
      role="dialog"
      aria-label={lang.attr('gui.at.point')}
    >
      {/* Only while it stands, the way the "+" popover is built: a closed
          popover has no rows, so nothing under it can take a click. */}
      {s.open ? (
        s.panel === 'bases' ? (
          <>
            <button className="prow chrome-at-row chrome-at-back" onClick={() => mentions.openRows()}>
              <span aria-hidden="true">{'‹'}</span>
              <span className="nm">{t('gui.at.bases')}</span>
            </button>
            <Bases s={s} />
          </>
        ) : (
          <>
            {/* The folder first, because it is the one every conversation has
                whether or not anybody set it. */}
            <button
              className="prow chrome-plus-row"
              onClick={() => {
                mentions.close()
                wd.toggle()
              }}
            >
              <Glyph d={FOLDER} />
              <span className="nm">{t('gui.at.folder')}</span>
            </button>
            <button className="prow chrome-plus-row" onClick={() => mentions.openBases()}>
              <Glyph d={BOOKS} />
              <span className="nm">{t('gui.at.bases')}</span>
              {s.picked.length > 0 && <span className="chrome-at-count">{s.picked.length}</span>}
            </button>
          </>
        )
      ) : null}
    </div>
  )
}

/* The button and its menu in one anchor, the way the "+" is. */
export function At(): JSX.Element {
  return (
    <span className="chrome-anch">
      <AtBtn />
      <AtPopover />
    </span>
  )
}
