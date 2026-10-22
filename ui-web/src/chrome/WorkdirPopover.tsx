/* The workspace popover: the default, the recent folders and a door into the
 * folder browser, then the browser itself, off the chip.
 *
 * Names only. A row says the folder's last segment and carries the path on its
 * title, because a reader picking between "thesis" and "raven" is reading
 * names, and the one who needs the path hovers. No heading either: the chip
 * the popover hangs off already says what is being chosen.
 *
 * It stands where the "+" menu stands and for the same reason: a child of the
 * chip's anchor, hung off the chip's top edge by the stylesheet, and never
 * moved. Nothing here reaches for an element, and every row carries a plain
 * onClick because the tree never leaves the container React delegates from.
 *
 * The menu's rows are the store's, built on open, so a folder pinned while the
 * popover stood does not reorder a list the reader is looking at. The browser
 * renders the listing the store holds, entry by entry: a folder the engine
 * would refuse is greyed and explained, and still a way in.
 */

import { useRef, useSyncExternalStore } from 'react'

import { composing } from '../features/composer/store'
import { t } from '../i18n/t'
import * as lang from '../state/lang'
import * as wd from '../state/workdir'

import type { DirListing } from '../features/workspace/types'
import type { JSX } from 'react'

/** The tick, drawn as the permission rows draw theirs. */
const CHECK = 'M5 12.5l4.5 4.5L19 7'
const HOME = 'M4 11.5 12 5l8 6.5V19a1 1 0 0 1-1 1h-4.5v-5h-5v5H5a1 1 0 0 1-1-1Z'
const UP = 'M12 19V6M6 12l6-6 6 6'
/* A stack of drives: two slabs, each with its light. */
const DRIVES = 'M4 6.5h16v4H4ZM4 13.5h16v4H4ZM7 8.5h.01M7 15.5h.01'

/* A long path shows its end, which names the folder the browser stands in --
   the reason the bar used to be drawn right to left. A field has to stay left
   to right to be typed into, so it is scrolled to its end as it mounts
   instead; it mounts again on every read, refused or not. */
function showEnd(el: HTMLInputElement | null): void {
  if (el) el.scrollLeft = el.scrollWidth
}
/* The chip's folder, spelled here rather than imported: the chip imports this
   file, and a glyph is not worth a cycle. */
/** A folder, in the same 24-unit outline the shield next door is drawn in.
 *  Here because this is where the folder is chosen; the tag beside a
 *  conversation's title and the "@" menu's row both draw the same one. */
export const FOLDER = 'M3.5 7.5A2 2 0 0 1 5.5 5.5h4l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z'

function Glyph({ d, className }: { d: string; className?: string }): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true" className={className}>
      <path d={d} />
    </svg>
  )
}

function Row({ row }: { row: wd.WdRow }): JSX.Element {
  return (
    <button
      className="prow"
      role="radio"
      aria-checked={row.ticked ? 'true' : 'false'}
      title={row.path ?? undefined}
      onClick={() => wd.pick(row.path)}
    >
      <span className="nm">{row.name}</span>
      {row.ticked ? <Glyph d={CHECK} className="tick" /> : null}
    </button>
  )
}

/* The menu: the default, a rule, the recent folders, then the row that opens
   the host's folder dialog (or the in-page browser, where the gateway is not
   this desktop or its host has no dialog), then whatever the last attempt had
   to say. The rule is drawn only when there is something under it. The native
   row takes no second press while a dialog it opened is still up -- the menu
   is closed by then, but a reader who opens it again must not raise a second
   dialog behind the first. */
function Menu({ rows, err, picking }: { rows: readonly wd.WdRow[]; err: string | null; picking: boolean }): JSX.Element {
  const [first, ...rest] = rows
  return (
    <>
      <div role="radiogroup" aria-label={t('gui.wd.title')}>
        {first ? <Row row={first} /> : null}
        {rest.length ? <div className="chrome-hr" /> : null}
        {rest.map((row) => <Row key={row.path ?? ''} row={row} />)}
      </div>
      <button className="prow chrome-wd-open" disabled={picking} onClick={() => void wd.chooseFolder()}>
        <span className="nm">{t(wd.nativePick() ? 'gui.wd.open' : 'gui.wd.open_remote')}</span>
      </button>
      {err ? <div className="note chrome-wd-err" role="alert">{err}</div> : null}
    </>
  )
}

/* The browser: where it stands, the folders under it, and the two ways out. A
   root has no parent, so where the gateway's machine has several roots
   (Windows drives) the bar offers the list of the others; the path itself is a
   field, so a folder can be named rather than walked to. */
function Browser({ at, loading, err, asked }: { at: DirListing; loading: boolean; err: string | null; asked: number }): JSX.Element {
  const roots = at.roots || []
  const field = useRef<HTMLInputElement>(null)
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
        {roots.length
          ? (
            <button className="chrome-wd-nav chrome-wd-drives" data-tip={t('gui.wd.drives')}
              aria-label={t('gui.wd.drives')} onClick={() => wd.drives()}>
              <Glyph d={DRIVES} />
            </button>
          )
          : null}
        {/* A field rather than a label: the walk cannot reach a folder nobody
            has listed (a dotfolder, one past the listing's cap), and the
            gateway takes any absolute path. Re-mounted on every read, refused
            or not, so a path that was not taken is replaced by wherever the
            browser actually stands. */}
        <input
          key={`${at.path}#${asked}`}
          ref={(el) => {
            field.current = el
            showEnd(el)
          }}
          className="chrome-wd-p chrome-wd-typed"
          type="text"
          defaultValue={at.path}
          title={at.path}
          aria-label={t('gui.wd.typed')}
          placeholder={t('gui.wd.typed')}
          spellCheck={false}
          onKeyDown={(e) => {
            if (e.key !== 'Enter' || composing(e.nativeEvent)) return
            e.preventDefault()
            void wd.go(e.currentTarget.value)
          }}
        />
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
          onClick={() => void wd.useHere(field.current?.value)}
        >
          {t('gui.wd.use')}
        </button>
      </div>
    </>
  )
}

/* The roots a root cannot reach by walking: the other drives, one row each. */
function Drives({ roots, err }: { roots: readonly { name: string; path: string; ok: boolean }[]; err: string | null }): JSX.Element {
  return (
    <>
      <div className="chrome-wd-list" role="list">
        {roots.map((root) => (
          <button
            key={root.path}
            role="listitem"
            className={root.ok ? 'prow' : 'prow chrome-wd-off'}
            title={root.ok ? root.path : t('gui.wd.blocked')}
            onClick={() => void wd.browse(root.path)}
          >
            <Glyph d={DRIVES} className="pico" />
            <span className="nm">{root.name}</span>
          </button>
        ))}
        {err ? <div className="note chrome-wd-err" role="alert">{err}</div> : null}
      </div>
      <div className="chrome-wd-foot">
        <button className="chrome-wd-btn" onClick={() => wd.back()}>{t('gui.wd.back')}</button>
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
      {/* Only while it stands: a closed popover has no rows, so nothing under
          it can take a click, and the served tree carries the shell alone. */}
      {s.open
        ? (s.view === 'browse' && s.listing
          ? <Browser at={s.listing} loading={s.loading} err={s.err} asked={s.asked} />
          : s.view === 'drives' && s.listing
            ? <Drives roots={s.listing.roots || []} err={s.err} />
            : <Menu rows={s.listed || []} err={s.err} picking={s.picking} />)
        : null}
    </div>
  )
}
