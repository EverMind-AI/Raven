/* The detail-sheet anatomy the three set-up-once pages share.
 *
 * Four parts in one order, so the reader learns the shape once:
 *
 *   head        who this is, one line of hard facts, and the overflow menu
 *   state line  what state it is in + the one action that state calls for
 *   content     the fields, or the tabs
 *   foot        whether there is anything unsaved, and one button to save it
 *
 * What this shape is a reaction to: a sheet that had grown into a heap of
 * form. Every field carried a standing hint under it, the description was
 * printed in the head and again in the field, and the verbs were scattered --
 * one in the head, one between two fields, two more in a strip at the bottom.
 * So: no standing hints (a field says what it is by its label; a validation
 * error is the only thing that appears under one), no fact printed twice, one
 * action position for the state, and destructive verbs behind the overflow menu
 * rather than sitting in the page's path.
 */

import { show as showMenu } from './menu'

import type { MenuItem } from './menu'
import type { JSX, ReactNode } from 'react'

/* The overflow button. Anchors the shared context menu (#menu) under itself, which
   is the same menu the session rows use -- one menu on the page, not a popup
   per sheet. */
export function SheetMenu({ items }: { items: Array<MenuItem | '-'> }): JSX.Element | null {
  if (!items.length) return null
  return (
    <button
      className="mini ghost sumenu"
      aria-label="…"
      onClick={(e) => {
        const r = (e.currentTarget as HTMLElement).getBoundingClientRect()
        showMenu(r.right - 4, r.bottom + 4, items)
      }}
    >
      ⋯
    </button>
  )
}

export function SheetHead({
  tile,
  name,
  tags,
  facts,
  menu,
  onClose,
  closeLabel,
}: {
  tile?: ReactNode
  name: string
  tags?: ReactNode
  /* One line of settled fact -- transport, last run, credential count. Set in
     the mono face because it is data, not prose. Never the description: that
     is editable content and appears once, in its own field. */
  facts?: string
  menu?: Array<MenuItem | '-'>
  /* Only a sheet that covers the page passes this. A sheet the page scrolls
     to is left by navigating away -- there is nothing to close -- but one
     drawn over a list has to hand back the list it covers. */
  onClose?: () => void
  closeLabel?: string
}): JSX.Element {
  return (
    <div className="suhead">
      {tile}
      <div className="meta">
        <div className="l1">
          <b>{name}</b>
          {tags}
        </div>
        {facts ? <div className="l2">{facts}</div> : null}
      </div>
      {menu && menu.length ? <SheetMenu items={menu} /> : null}
      {onClose ? (
        <button className="mini ghost suclose" aria-label={closeLabel || 'close'} onClick={onClose}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" aria-hidden="true">
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>
      ) : null}
    </div>
  )
}

/* State and its one action, on one line. The sheet's only action position:
   whatever the state calls for lands here, so the reader never has to scan the
   sheet for the verb that applies. */
export function StateLine({
  cls,
  text,
  act,
}: {
  cls: string
  text: string
  act?: ReactNode
}): JSX.Element {
  return (
    <div className="sustate">
      <span className={'led' + (cls === 'ok' ? '' : ' ' + cls)} />
      <span className={cls === 'bad' ? 'badtx' : cls === 'warn' ? 'warntx' : ''}>{text || '—'}</span>
      {act ? <span className="a">{act}</span> : null}
    </div>
  )
}

/* One field. No hint slot: the label says what it is, and `err` is the only
   thing that may appear under the input -- which means anything that does
   appear there is worth reading. */
export function Field({
  label,
  title,
  err,
  children,
}: {
  label: string
  /* The label's tooltip, for the one thing that is worth having but not worth
     a line: the raw config key behind a translated label, or the schema's own
     paragraph when it was too long to be a label. */
  title?: string
  err?: string
  children: ReactNode
}): JSX.Element {
  return (
    <div className="sufield">
      <label title={title}>{label}</label>
      {children}
      {err ? <div className="e">{err}</div> : null}
    </div>
  )
}

export function SheetFoot({
  note,
  label,
  onSave,
  disabled,
}: {
  note?: string
  label: string
  onSave: () => void
  disabled?: boolean
}): JSX.Element {
  return (
    <div className="sufoot">
      <span className="n">{note || ''}</span>
      <button className="mini key" disabled={!!disabled} onClick={onSave}>
        {label}
      </button>
    </div>
  )
}
