/* The working-directory popover: the default, the recent folders and a door
 * into the folder browser, then the browser itself, over the composer.
 *
 * It stands where the slash palette stands and for the same reason: a child of
 * the composer card, positioned by the card's own `.pop` rule, and never moved.
 * The permission and tier popovers leave the card for the body because they
 * are placed with fixed coordinates, which the card's entrance animation
 * re-bases; this one hangs off the card's top edge with the stylesheet's
 * `bottom: calc(100% + 8px)`, so the card is exactly the box it should be
 * measured against, and nothing here reaches for an element. That is also what
 * lets every row carry a plain onClick: the tree never leaves the container
 * React delegates from.
 *
 * The menu's rows are the store's, built on open, so a folder pinned while the
 * popover stood does not reorder a list the reader is looking at. The browser
 * renders the listing the store holds, entry by entry: a folder the engine
 * would refuse is greyed and explained, and still a way in.
 */

import { useSyncExternalStore } from 'react'

import { t } from '../i18n/t'
import * as lang from '../state/lang'
import * as wd from '../state/workdir'
import { FOLDER } from './WorkdirChip'

import type { DirListing } from '../features/workspace/types'
import type { JSX } from 'react'

/** The tick, drawn as the permission rows draw theirs. */
const CHECK = 'M5 12.5l4.5 4.5L19 7'
const HOME = 'M4 11.5 12 5l8 6.5V19a1 1 0 0 1-1 1h-4.5v-5h-5v5H5a1 1 0 0 1-1-1Z'
const UP = 'M12 19V6M6 12l6-6 6 6'

function Glyph({ d, className }: { d: string; className?: string }): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true" className={className}>
      <path d={d} />
    </svg>
  )
}

/* The menu: the default and the recent folders as a radio group, then the row
   that opens the browser, then whatever the last attempt to browse had to say. */
function Menu({ rows, err }: { rows: readonly wd.WdRow[]; err: string | null }): JSX.Element {
  return (
    <>
      <div role="radiogroup" aria-label={t('gui.wd.title')}>
        {rows.map((row) => (
          <button
            key={row.path ?? ''}
            className="prow"
            role="radio"
            aria-checked={row.ticked ? 'true' : 'false'}
            title={row.path ?? undefined}
            onClick={() => wd.pick(row.path)}
          >
            {/* The path is shown from its end when it overflows, so it wears the
                same left-to-right marks the browser's path bar does. */}
            <span className="txt"><span className="nm">{row.name}</span><span className="sub">{'\u200e'}{row.sub}{'\u200e'}</span></span>
            {row.ticked ? <Glyph d={CHECK} className="tick" /> : null}
          </button>
        ))}
      </div>
      <button className="prow chrome-wd-open" onClick={() => void wd.browse()}>
        <span className="txt"><span className="nm">{t('gui.wd.open')}</span></span>
      </button>
      {err ? <div className="note chrome-wd-err" role="alert">{err}</div> : null}
    </>
  )
}

/* The browser: where it stands, the folders under it, and the two ways out. */
function Browser({ at, loading, err }: { at: DirListing; loading: boolean; err: string | null }): JSX.Element {
  return (
    <>
      <div className="chrome-wd-path">
        <button className="chrome-wd-nav" data-tip={t('gui.wd.home')} aria-label={t('gui.wd.home')}
          onClick={() => void wd.browse(at.home)}>
          <Glyph d={HOME} />
        </button>
        <button className="chrome-wd-nav" data-tip={t('gui.wd.up')} aria-label={t('gui.wd.up')}
          disabled={!at.parent} onClick={() => { if (at.parent) void wd.browse(at.parent) }}>
          <Glyph d={UP} />
        </button>
        {/* Wrapped in left-to-right marks: the path is shown from its end when
            it overflows (direction: rtl in the stylesheet), and a bidi-neutral
            separator at either end would otherwise flip it. */}
        <span className="chrome-wd-p" title={at.path}>{'‎'}{at.path}{'‎'}</span>
      </div>
      <div className="chrome-wd-list" role="list">
        {at.entries.map((e) => (
          <button
            key={e.path}
            role="listitem"
            className={e.ok ? 'prow' : 'prow chrome-wd-off'}
            title={e.ok ? e.path : t('gui.wd.blocked')}
            onClick={() => void wd.browse(e.path)}
          >
            <Glyph d={FOLDER} className="pico" />
            <span className="nm">{e.name}</span>
          </button>
        ))}
        {!at.entries.length && !loading ? <div className="note">{t('gui.wd.empty')}</div> : null}
        {loading ? <div className="note">{t('gui.wd.loading')}</div> : null}
        {err ? <div className="note chrome-wd-err" role="alert">{err}</div> : null}
      </div>
      <div className="chrome-wd-foot">
        <button className="chrome-wd-btn" onClick={() => wd.back()}>{t('gui.wd.back')}</button>
        <button
          className="chrome-wd-btn chrome-wd-use"
          disabled={!at.ok}
          title={at.ok ? at.path : t('gui.wd.blocked')}
          onClick={() => wd.useHere()}
        >
          {t('gui.wd.use')}
        </button>
      </div>
    </>
  )
}

export function WorkdirPopover(): JSX.Element {
  const s = useSyncExternalStore(wd.subscribe, wd.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <div
      className="pop"
      id="wdPop"
      data-open={s.open ? 'true' : 'false'}
      data-view={s.view}
      role="dialog"
      aria-label={lang.attr('gui.wd.title')}
    >
      <div className="hd"><span className="lab">{t('gui.wd.title')}</span></div>
      {/* Only while it stands: a closed popover has no rows, so nothing under
          it can take a click, and the served tree carries the heading alone. */}
      {s.open
        ? (s.view === 'browse' && s.listing
          ? <Browser at={s.listing} loading={s.loading} err={s.err} />
          : <Menu rows={s.listed || []} err={s.err} />)
        : null}
    </div>
  )
}
