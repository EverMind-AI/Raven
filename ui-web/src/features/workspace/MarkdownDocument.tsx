/* A Markdown file read as a document rather than as an answer: the text keeps
 * its own heading levels, gains an outline that follows the reader down the
 * page, and its footnotes and `#anchors` become links inside itself.
 *
 * The markup comes from lib/prose.ts's mdDocument -- a pure string, like every
 * other piece of prose this page draws. What is here is only what a string
 * cannot do: which heading the reader is at, and what clicking an anchor does.
 * The outline is drawn twice (a column and a fold-away) because the two are
 * one list at two widths; the sheet shows one at a time.
 */

import { useEffect, useMemo, useRef, useState } from 'react'

import { t } from '../../i18n/t'
import { mdDocument } from '../../lib/prose'

import type { JSX, MouseEvent } from 'react'

/** The nearest ancestor that scrolls, or null when the window owns the scroll
 *  (a page of its own, as a file preview window is). */
function scrollParent(node: HTMLElement): HTMLElement | null {
  for (let parent = node.parentElement; parent; parent = parent.parentElement) {
    if (/(auto|scroll)/.test(getComputedStyle(parent).overflowY)) return parent
  }
  return null
}

export function MarkdownDocument({ text }: { text: string }): JSX.Element {
  const footnotes = t('gui.ws.footnotes')
  const doc = useMemo(() => mdDocument(text, 'mdoc', footnotes), [text, footnotes])
  const article = useRef<HTMLElement>(null)
  const compact = useRef<HTMLDetailsElement>(null)
  const [active, setActive] = useState('')

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
    <div className="workspace-mddoc" data-outline={doc.headings.length > 0 ? '1' : undefined}>
      {doc.headings.length > 0 ? (
        <>
          <aside className="workspace-md-outline">
            <div className="workspace-md-outline-title">{outline}</div>
            {links('wide')}
          </aside>
          <details className="workspace-md-compact" ref={compact}>
            <summary>{outline}</summary>
            <div className="workspace-md-pop">{links('compact')}</div>
          </details>
        </>
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
    </div>
  )
}
