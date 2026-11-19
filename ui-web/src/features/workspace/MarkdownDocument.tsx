/* A Markdown file read as a document rather than as an answer: the text keeps
 * its own heading levels, gains an outline that follows the reader down the
 * page, and its footnotes and `#anchors` become links inside itself.
 *
 * The markup comes from lib/prose.ts's mdDocument -- a pure string, like every
 * other piece of prose this page draws. What is here is only what a string
 * cannot do: which heading the reader is at, what clicking an anchor does, and
 * the one measurement the layout needs (whether the pane is wide enough for
 * the outline's own column; narrow, the same list is a menu in the header).
 *
 * The layout, the type scale and the two breakpoints are the EverMe file
 * preview's document reader, ported. The breakpoints are measured rather than
 * spelled as media queries: the document lives in a pane, so the window says
 * nothing about the room it has, and the desktop shell runs on a WebKit that
 * predates container queries.
 */

import { LeftToRightListBulletIcon } from '@hugeicons/core-free-icons'
import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { Icon } from '../../components/Icon'
import { t } from '../../i18n/t'
import { mdDocument } from '../../lib/prose'

import type { JSX, MouseEvent } from 'react'

/** The outline gets its own column from this width up. */
const WIDE = 900
/** Under this width the menu's button keeps only its glyph. */
const TIGHT = 380

/** The nearest ancestor that scrolls, or null when the window owns the scroll
 *  (a page of its own, as a file preview window is). */
function scrollParent(node: HTMLElement): HTMLElement | null {
  for (let parent = node.parentElement; parent; parent = parent.parentElement) {
    if (/(auto|scroll)/.test(getComputedStyle(parent).overflowY)) return parent
  }
  return null
}

export function MarkdownDocument({ text, menuSlot }: {
  text: string
  /** The header box the fold-away outline mounts into, when the bar has one. */
  menuSlot?: HTMLElement | null
}): JSX.Element {
  const footnotes = t('gui.ws.footnotes')
  const doc = useMemo(() => mdDocument(text, 'mdoc', footnotes), [text, footnotes])
  const frame = useRef<HTMLDivElement>(null)
  const article = useRef<HTMLElement>(null)
  const compact = useRef<HTMLDetailsElement>(null)
  const [active, setActive] = useState('')
  const [wide, setWide] = useState(false)
  const [tight, setTight] = useState(false)

  useEffect(() => {
    const root = frame.current
    if (!root) return
    /* The two container-relative lengths the reader is set in: the gap
       between the outline and the text, and the title's size, both 4.1% of
       the box they sit in. */
    const read = (): void => {
      const width = root.clientWidth
      setWide(width >= WIDE)
      setTight(width <= TIGHT)
      const share = Math.round(width * 0.041)
      root.style.setProperty('--md-gap', `${Math.min(64, Math.max(32, share))}px`)
      root.style.setProperty('--md-h1', `${Math.min(54, Math.max(43, share))}px`)
    }
    read()
    if (typeof ResizeObserver === 'undefined') return
    const watcher = new ResizeObserver(read)
    watcher.observe(root)
    return () => watcher.disconnect()
  }, [])

  useEffect(() => {
    const root = article.current
    if (!root) return
    const items = Array.from(root.querySelectorAll<HTMLElement>('[data-mh]'), (element) => element.id)
    if (!items.length) return
    const scroller = scrollParent(root)
    const boundaryOf = (): number => (scroller?.getBoundingClientRect().top ?? 0) + 68
    let frame = 0
    const update = (): void => {
      frame = 0
      const boundary = boundaryOf()
      let current = items[0]!
      for (const id of items) {
        const element = root.ownerDocument.getElementById(id)
        if (!element) continue
        if (element.getBoundingClientRect().top <= boundary) current = id
        else break
      }
      setActive(current)
    }
    const schedule = (): void => {
      if (!frame) frame = requestAnimationFrame(update)
    }
    update()
    const target: EventTarget = scroller ?? window
    target.addEventListener('scroll', schedule, { passive: true })
    const watcher = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(schedule)
    watcher?.observe(root)
    return () => {
      target.removeEventListener('scroll', schedule)
      watcher?.disconnect()
      cancelAnimationFrame(frame)
    }
  }, [doc])

  function jump(event: MouseEvent, id: string): void {
    if (event.button !== 0 || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
    const target = document.getElementById(id)
    if (!target || !article.current?.contains(target)) return
    event.preventDefault()
    if (compact.current) compact.current.open = false
    target.scrollIntoView?.()
    target.focus({ preventScroll: true })
  }

  const outline = t('gui.ws.outline')
  const sectionLevel = doc.headings.reduce((min, heading) => Math.min(min, heading.level), 6)
  const links = (where: string): JSX.Element => (
    <nav className="workspace-md-links" aria-label={outline}>
      {doc.headings.map((heading) => (
        <a
          key={`${where}:${heading.id}`}
          href={`#${heading.id}`}
          aria-current={heading.id === active ? 'location' : undefined}
          className={heading.level > sectionLevel ? 'workspace-md-nested' : undefined}
          onClick={(event) => jump(event, heading.id)}
        >
          {heading.label}
        </a>
      ))}
    </nav>
  )

  return (
    <div
      ref={frame}
      className="workspace-mddoc"
      data-outline={doc.headings.length > 0 ? '1' : undefined}
      data-wide={wide ? '1' : undefined}
      data-tight={tight ? '1' : undefined}
    >
      {doc.headings.length > 0 ? (
        <aside className="workspace-md-outline">
          <div className="workspace-md-outline-title">
            <Icon icon={LeftToRightListBulletIcon} size={14} />
            {outline}
          </div>
          {links('wide')}
        </aside>
      ) : null}
      <article
        ref={article}
        className="prose workspace-md-body"
        onClick={(event) => {
          const href = (event.target instanceof Element ? event.target.closest('a') : null)?.getAttribute('href')
          if (href?.startsWith('#')) jump(event, href.slice(1))
        }}
        dangerouslySetInnerHTML={{ __html: doc.html }}
      />
      {menuSlot && !wide && doc.headings.length > 0
        ? createPortal(
          <details className="workspace-md-menu" ref={compact} data-tight={tight ? '1' : undefined}>
            <summary>
              <Icon icon={LeftToRightListBulletIcon} size={15} />
              <span>{outline}</span>
            </summary>
            <div className="workspace-md-pop">{links('menu')}</div>
          </details>,
          menuSlot,
        )
        : null}
    </div>
  )
}
