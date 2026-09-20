/* The document beside its chunks, with the piece a reader clicked drawn on it.
 *
 * A frame around the browser's own PDF viewer is most of what this needs and
 * exactly one thing short of it. That response is sandboxed to an opaque
 * origin, so nothing on this page can reach inside it: `#page=N` is the only
 * control there is, and a chunk is not a page. It is a few regions on one --
 * a paragraph, a table, sometimes two of them either side of a page break --
 * and the question a reader has when a retrieved passage looks wrong is which
 * part of the page it came from.
 *
 * So the pages are drawn here instead, through pdf.js, and the regions are
 * ordinary elements laid over them. Coordinates arrive in the page's own
 * points with the origin at the top left, which is the frame pdf.js lays a
 * viewport out in, so the only conversion is the scale.
 *
 * The frame is still the fallback. pdf.js is a megabyte of worker that can
 * fail to load for reasons this page cannot see, and a reader who cannot read
 * their document is worse off than one who cannot see a highlight.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { JSX } from 'react'

import { loadPdfjs } from './pdfjs'
import type { PdfDocument } from './pdfjs'

import { t } from '../../shell/bridge'
import type { KbChunkRegion } from './types'

/* How far down the scroller a focused region is parked. Not the very top: a
   region flush against the edge reads as cut off, and the line above it is
   usually what says what the region is. */
const MARGIN_PX = 64

/* Pages drawn ahead of and behind the viewport. Drawing is the expensive part
   -- a page is a canvas the size of the panel -- so a long document draws what
   is on screen and a screen either side, not three hundred pages. */
const ROOT_MARGIN = '200% 0px'

interface Sheet {
  /* 1-based, as every page number in this tree is. */
  number: number
  width: number
  height: number
}

export function PdfView({
  url,
  framed,
  title,
  page,
  regions,
  focus,
}: {
  url: string
  /* The same document for a frame to show, carrying `#page=N`. Separate from
     `url` because the fragment is the whole of what a sandboxed viewer can be
     told, and pdf.js must not be handed one -- it fetches the bytes. */
  framed: string
  title: string
  /* The page the piece starts on, for a piece that knows its page and not its
     place on it -- a slide, or a parser that recorded one and not the other.
     The top of that page is the right answer there; it is what a framed viewer
     could be told, and it must not get worse for being drawn instead. */
  page: number | null
  /* The regions of the piece a reader clicked, across every page it touches. */
  regions: KbChunkRegion[]
  /* Changes on every click, including a second click on the same piece, so
     asking again scrolls again. */
  focus: number
}): JSX.Element {
  const scroller = useRef<HTMLDivElement>(null)
  const doc = useRef<PdfDocument | null>(null)
  const [sheets, setSheets] = useState<Sheet[]>([])
  const [scale, setScale] = useState(0)
  const [failed, setFailed] = useState(false)

  /* Open the document and measure its pages. Measuring is metadata rather than
     drawing, so it is cheap even for a long file, and it is what lets every
     page reserve its own height before anything is drawn -- without that the
     scroller grows as pages arrive and a scroll to page 40 lands somewhere
     else by the time it is drawn. */
  useEffect(() => {
    let dropped = false
    const width = scroller.current?.clientWidth ?? 0
    void (async () => {
      try {
        const pdfjs = await loadPdfjs()
        const opened = await pdfjs.getDocument({ url }).promise
        if (dropped) return
        doc.current = opened
        const first = await opened.getPage(1)
        const natural = first.getViewport({ scale: 1 })
        /* One scale for the document, from the panel's width. A PDF whose
           pages differ in size is rare and reads correctly either way; a scale
           per page would make the column ragged. */
        const fitted = width > 0 ? (width - 24) / natural.width : 1
        const measured: Sheet[] = []
        for (let n = 1; n <= opened.numPages; n += 1) {
          const page = n === 1 ? first : await opened.getPage(n)
          const view = page.getViewport({ scale: fitted })
          measured.push({ number: n, width: view.width, height: view.height })
        }
        if (dropped) return
        setScale(fitted)
        setSheets(measured)
      } catch {
        /* Whatever went wrong -- the worker, the bytes, a format pdf.js will
           not open -- the document itself is still servable in a frame. */
        if (!dropped) setFailed(true)
      }
    })()
    return () => {
      dropped = true
      doc.current = null
    }
  }, [url])

  /* Take the scroller to the first region of the piece that was clicked.
     Layout effect, so it runs against the heights the sheets already reserved
     rather than after a paint at the old position. */
  useLayoutEffect(() => {
    const box = scroller.current
    if (!box || !sheets.length) return
    const first = [...regions].sort((a, b) => a.page_number - b.page_number || a.top - b.top)[0]
    const number = first ? first.page_number : page
    if (number === null) return
    const sheet = sheets.find((s) => s.number === number)
    if (!sheet) return
    const above = sheets.filter((s) => s.number < sheet.number).reduce((sum, s) => sum + s.height + 8, 0)
    /* To the region where there is one, to the top of the page where there is
       not. A page with no region to point at is scrolled flush, because the
       margin below the top exists to keep a highlight off the edge. */
    const top = first ? Math.max(0, above + first.top * scale - MARGIN_PX) : above
    /* `scrollTo` where there is one, the property otherwise: the smooth scroll
       is the nicety and landing on the region is the point. */
    if (typeof box.scrollTo === 'function') box.scrollTo({ top, behavior: 'smooth' })
    else box.scrollTop = top
  }, [focus, sheets, regions, page, scale])

  if (failed) {
    /* The frame, and with it `#page=N`, which is the whole of what a sandboxed
       viewer can be told. Keyed on the page so a new one re-navigates it. */
    return <iframe key={page ?? 0} className="kbframe" src={framed} title={title} />
  }
  if (!sheets.length) {
    return (
      <div className="kbpdf" ref={scroller}>
        <div className="empty-note">
          <div className="ttl">{t('gui.kb.preview_loading')}</div>
        </div>
      </div>
    )
  }
  return (
    <div className="kbpdf" ref={scroller}>
      {sheets.map((sheet) => (
        <PdfPage
          key={sheet.number}
          sheet={sheet}
          doc={doc}
          scale={scale}
          regions={regions.filter((r) => r.page_number === sheet.number)}
        />
      ))}
    </div>
  )
}

function PdfPage({
  sheet,
  doc,
  scale,
  regions,
}: {
  sheet: Sheet
  doc: React.RefObject<PdfDocument | null>
  scale: number
  regions: KbChunkRegion[]
}): JSX.Element {
  const holder = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  const [near, setNear] = useState(false)
  const drawn = useRef(false)

  useEffect(() => {
    const node = holder.current
    if (!node || typeof IntersectionObserver !== 'function') {
      setNear(true)
      return
    }
    const watch = new IntersectionObserver(
      (entries) => entries.some((e) => e.isIntersecting) && setNear(true),
      { root: null, rootMargin: ROOT_MARGIN },
    )
    watch.observe(node)
    return () => watch.disconnect()
  }, [])

  const draw = useCallback(async () => {
    const opened = doc.current
    const target = canvas.current
    if (!opened || !target || drawn.current) return
    drawn.current = true
    try {
      const page = await opened.getPage(sheet.number)
      /* Drawn at the device's own pixel density and shown at CSS size, or
         every glyph on a retina display is drawn once and then scaled up. */
      const ratio = window.devicePixelRatio || 1
      const view = page.getViewport({ scale: scale * ratio })
      target.width = Math.round(view.width)
      target.height = Math.round(view.height)
      await page.render({ canvas: target, viewport: view }).promise
    } catch {
      /* One page failing to draw is a blank page, not a broken document. */
      drawn.current = false
    }
  }, [doc, scale, sheet.number])

  useEffect(() => {
    if (near) void draw()
  }, [near, draw])

  return (
    <div className="kbpdfpage" ref={holder} style={{ width: sheet.width, height: sheet.height }}>
      <canvas ref={canvas} style={{ width: sheet.width, height: sheet.height }} />
      {regions.map((region, index) => (
        <div
          key={index}
          className="kbpdfmark"
          style={{
            left: region.x0 * scale,
            top: region.top * scale,
            width: Math.max(2, (region.x1 - region.x0) * scale),
            height: Math.max(2, (region.bottom - region.top) * scale),
          }}
        />
      ))}
    </div>
  )
}
