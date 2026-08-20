import { useEffect, useRef, useSyncExternalStore } from 'react'

import { shell, t } from '../../shell/bridge'
import * as store from './store'

import type { Attachment, SlashCmd } from './types'
import type { ReactElement } from 'react'

/* The dock's four drawn collections. Each is its own root over the container
 * page.html already carries -- #queued, #atts, #slashList, and a host at the
 * tail of #stage for the live turn row -- so the markup, the class names and
 * the layout read exactly as the renderer they replace.
 *
 * The static single elements of the dock (the field, the send button, the
 * palette's popover shell, the back-to-bottom pill) stay markup and are
 * written by the store: they are one element each, reached by id from half the
 * page, and their whole state is a handful of attributes.
 */

const PEN = 'M4.5 19.5h4L19 9a2.12 2.12 0 0 0-3-3L5.5 16.5v3ZM15.5 6.5l2 2'
const CROSS = 'M6.5 6.5l11 11M17.5 6.5l-11 11'

function useComposer(): number {
  return useSyncExternalStore(store.subscribe, () => store.getState().v)
}

/* An icon-only button whose verb lives in the hover pill ([data-tip]). */
function IcBtn({ label, path, onClick }: {
  label: string; path: string; onClick: () => void
}): ReactElement {
  return (
    <button className="icb" data-tip={label} aria-label={label} onClick={onClick}
      dangerouslySetInnerHTML={{
        __html: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${path}"/></svg>`,
      }} />
  )
}

/* ── queued messages ──────────────────────────────────────────────────── */

/* Uncontrolled on purpose: the text belongs to the input until it is
   committed, so typing in a queued row repaints nothing. Escape has to disarm
   the blur commit before it lands, which is what the ref is for. */
function QueueEdit({ text, i }: { text: string; i: number }): ReactElement {
  const ref = useRef<HTMLInputElement | null>(null)
  const cancelled = useRef(false)
  useEffect(() => {
    const e = ref.current
    if (!e) return
    e.focus()
    e.select()
  }, [])
  const done = (commit: boolean): void => {
    if (commit) store.commitRow(i, ref.current ? ref.current.value : '')
    else store.cancelRow()
  }
  return (
    <input ref={ref} defaultValue={text}
      onBlur={() => { if (!cancelled.current) done(true) }}
      onKeyDown={(e) => {
        if (store.composing(e.nativeEvent)) return
        if (e.key === 'Enter') {
          e.preventDefault()
          done(true)
        }
        if (e.key === 'Escape') {
          cancelled.current = true
          done(false)
        }
      }} />
  )
}

export function QueueList(): ReactElement {
  useComposer()
  const editing = store.getState().editing
  return (
    <>
      {store.queue().map((text, i) => (
        <div className="qrow" key={i}>
          {editing === i ? <QueueEdit text={text} i={i} /> : <span className="v">{text}</span>}
          <IcBtn label={t('gui.q.edit')} path={PEN} onClick={() => store.editRow(i)} />
          <IcBtn label={t('gui.q.remove')} path={CROSS} onClick={() => store.removeRow(i)} />
        </div>
      ))}
    </>
  )
}

/* ── the attachment tray ──────────────────────────────────────────────── */

function AttChip({ a, i }: { a: Attachment; i: number }): ReactElement {
  const isImg = !!a.url
  const size = a.uploading ? t('gui.att.uploading') : store.fmtSize(a.size)
  return (
    <div className={'att' + (isImg ? ' img' : '') + (a.uploading ? ' up' : '')}>
      {isImg ? (
        /* the square crops the image, so a click has to be able to show all of it */
        <img src={a.url as string} alt={a.name} title={`${a.name} · ${size}`}
          onClick={() => shell().openImage?.(a.url as string, a.name)} />
      ) : (
        <>
          <span className="nm">{a.name}</span>
          <span className="sz">{size}</span>
        </>
      )}
      <button className="rm" aria-label={t('gui.att.remove', { name: a.name })}
        onClick={(e) => { e.stopPropagation(); store.removeAtt(i) }}>✕</button>
    </div>
  )
}

export function AttTray(): ReactElement {
  useComposer()
  return <>{store.getState().atts.map((a, i) => <AttChip key={i} a={a} i={i} />)}</>
}

/* ── the slash palette ────────────────────────────────────────────────── */

function SlashRow({ x, on }: { x: SlashCmd; on: boolean }): ReactElement {
  return (
    <button className="srow" role="option" aria-selected={String(on) as 'true' | 'false'}
      onMouseDown={(e) => { e.preventDefault(); store.runSlash(x) }}>
      <span className="cmd">{store.slashCmd(x)}</span>
      <span className="d">{store.slashDesc(x)}</span>
    </button>
  )
}

export function SlashList(): ReactElement {
  useComposer()
  const s = store.getState()
  return <>{s.slashRows.map((x, i) => <SlashRow key={x.id} x={x} on={i === s.slashSel} />)}</>
}

/* ── the live turn row ────────────────────────────────────────────────── */

/* Decorative to a screen reader -- whatever it sits beside carries the meaning
   in words, and three animated bars announced as anything would be noise on a
   row that repaints four times a second. */
export function WorkGlyph(): ReactElement {
  return <span className="wkg" aria-hidden="true"><i /><i /><i /></span>
}

/* The clock, and nothing else. It used to switch between three words
   (starting / writing / working) for a distinction the reader cannot act on:
   whether the stream has produced a token yet does not change what to do next,
   and the glyph already says the turn is alive. The word survives as the row's
   accessible name, where a reader who gets the row as text still needs it. */
export function TurnLive({ afterPaint }: { afterPaint?: () => void }): ReactElement | null {
  useComposer()
  const live = store.getState().live
  useEffect(() => { if (afterPaint) afterPaint() })
  if (!live) return null
  const clock = store.durText(store.liveMs())
  return (
    <div className="turnlive" aria-live="off" aria-label={t('gui.live.busy', { t: clock })}>
      <WorkGlyph />
      <span className="lb">{clock}</span>
    </div>
  )
}
