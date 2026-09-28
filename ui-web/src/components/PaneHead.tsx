/* The one header a desk pane has.
 *
 * Every pane used to say its name twice: once in the window's own header, and
 * again in a bar the content drew under it -- the file viewer's path bar, a
 * task's status line, a node card's back-and-title row. Two rows, one name,
 * and the content's row carried the controls the header had room for.
 *
 * So the header is the only place a pane is named, and it always speaks for
 * what the pane is showing NOW: a file, a task, or one node of a task, read as
 * a trail back to the task it belongs to. Left to right, the same slots for
 * every kind -- a glyph (or the way back), the name with a muted line of facts
 * under it, then the controls that act on it. The facts get a line of their
 * own because a pane is usually narrow: beside the name they were the first
 * thing squeezed out. The window's own two controls
 * (fullscreen, close) stay the desk's, after a hairline.
 *
 * The content draws this, not the desk, because the content owns what it
 * says: which node is picked, which tab is up, whether a file was granted a
 * run. The desk hands down where the header is (`PaneHeadSlot`) and this
 * portals into it. Without a slot -- the same view drawn outside a pane -- it
 * renders in place.
 *
 * `pane-head*` classes are this file's (scripts/check-class-namespace.mjs);
 * the rules are in src/styles/page.css beside the pane they sit in.
 */
import { createContext, useContext } from 'react'
import { createPortal } from 'react-dom'

import type { JSX, ReactNode } from 'react'

export interface PaneHeadTarget {
  el: HTMLElement | null
  /* The pane kind's glyph, which the desk knows and the content does not. */
  icon: ReactNode
}

export const PaneHeadSlot = createContext<PaneHeadTarget | null>(null)

export const usePaneHead = (): PaneHeadTarget | null => useContext(PaneHeadSlot)

const Chevron = (): JSX.Element => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
    <path d="M15 5l-7 7 7 7" />
  </svg>
)

export interface PaneHeadProps {
  title: string
  /* The full name, when the title is a shortened one (a file's base name). */
  tip?: string
  /* Set while the pane shows a part of something: the glyph becomes the way
     back, and `parent` -- the whole it is a part of -- leads the title as a
     crumb that goes back too. A parent that reads the same as the title is not
     drawn: a single spawn's one node is named after its task. */
  back?: { label: string; onBack: () => void }
  parent?: string
  meta?: ReactNode
  icon?: ReactNode
  titleRef?: (el: HTMLElement | null) => void
  children?: ReactNode
}

function Parts({ title, tip, back, parent, meta, icon, titleRef, children }: PaneHeadProps): JSX.Element {
  const crumb = back && parent && parent !== title ? parent : null
  return (
    <>
      {back ? (
        <button type="button" className="pane-head-back" onClick={back.onBack} aria-label={back.label} title={back.label}>
          <Chevron />
        </button>
      ) : <span className="pane-head-glyph">{icon}</span>}
      <span className="pane-head-id">
        <span className="pane-head-line">
          {crumb ? (
            <>
              <button type="button" className="pane-head-crumb" onClick={back!.onBack} title={crumb}>{crumb}</button>
              <span className="pane-head-sep" aria-hidden="true">{'\u203a'}</span>
            </>
          ) : null}
          <b className="pane-head-title" title={tip ?? title} ref={titleRef}>{title}</b>
        </span>
        {meta ? <span className="pane-head-meta">{meta}</span> : null}
      </span>
      <span className="pane-head-acts">{children}</span>
    </>
  )
}

export function PaneHead(props: PaneHeadProps): JSX.Element | null {
  const slot = usePaneHead()
  if (!slot) return <div className="pane-head"><Parts {...props} /></div>
  if (!slot.el) return null
  return createPortal(<Parts {...props} icon={props.icon ?? slot.icon} />, slot.el)
}

/* The desk's own head for the kinds whose content does not draw one. */
export function PaneHeadParts(props: PaneHeadProps): JSX.Element {
  return <Parts {...props} />
}

/* A pane's view switch -- rendered or source, context or work order. One
   shape for every kind, so the header reads the same whichever pane it tops. */
export function PaneHeadSeg<K extends string>({ options, value, onPick, label }: {
  options: Array<{ key: K; label: string }>
  value: K
  onPick: (key: K) => void
  label?: string
}): JSX.Element {
  return (
    <span className="pane-head-seg" role="tablist" aria-label={label}>
      {options.map((o) => (
        <button key={o.key} type="button" role="tab" aria-selected={o.key === value} onClick={() => onPick(o.key)}>
          {o.label}
        </button>
      ))}
    </span>
  )
}
