/** The shared browser in miniature: the palette's Browser tab.
 *
 * Pictures of the pages, not a list about them. The reader glances at the
 * palette to see what the model is doing; a row saying "httpbin.org/post" made
 * them open a window to find out, and the window is the thing they were trying
 * not to open.
 *
 * One card per tab, because two agents browse in two tabs and only one of them
 * streams: the front tab draws live frames, the others keep the last picture
 * seen of them, so both pages are on screen at once instead of one pane
 * flickering between them. A card carries the number of the agent working in
 * its tab, which is the fact the strip could not state before. Clicking a card
 * brings its tab forward and opens the window for real.
 */

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../shell/bridge'
import * as store from './store'

import type { BrowserTabRow } from './types'
import type { JSX, ReactNode, RefObject } from 'react'

/* Sized to the frame, scaled by the stylesheet: the cheapest sharp thumbnail,
   and one that follows the viewport when the pane resizes the page. Decode
   with the stage's backpressure -- arrivals during a decode replace the pending
   frame rather than queue behind it. Only this card's own url is drawn: the
   stream carries whichever tab is in front, and a card that painted every
   frame would show its neighbour's page. */
function useMirror(canvas: RefObject<HTMLCanvasElement | null>, url: string, onDraw: () => void): void {
  useEffect(() => {
    let pending: Blob | null = null
    let busy = false
    const draw = async (): Promise<void> => {
      if (busy || typeof createImageBitmap !== 'function') return
      busy = true
      while (pending) {
        const blob = pending
        pending = null
        try {
          const bmp = await createImageBitmap(blob)
          const cv = canvas.current
          if (cv) {
            if (cv.width !== bmp.width || cv.height !== bmp.height) {
              cv.width = bmp.width
              cv.height = bmp.height
            }
            cv.getContext('2d')?.drawImage(bmp, 0, 0)
            onDraw()
          }
          bmp.close()
        } catch {
          /* a torn frame; the next one replaces it */
        }
      }
      busy = false
    }
    const seen = store.shotFor(url)
    if (seen) {
      pending = seen
      void draw()
    }
    return store.subscribeFrames((blob, at) => {
      if (at !== url) return
      pending = blob
      void draw()
    })
  }, [canvas, url, onDraw])
}

function PipCard({ tab, onOpen }: { tab: BrowserTabRow; onOpen: () => void }): JSX.Element {
  const canvas = useRef<HTMLCanvasElement | null>(null)
  const [blank, setBlank] = useState(() => !store.shotFor(tab.url))
  useMirror(
    canvas,
    tab.url,
    useCallback(() => setBlank(false), []),
  )
  return (
    <button
      type="button"
      className="desk-pip"
      data-live={tab.active ? '1' : undefined}
      data-blank={blank ? '1' : undefined}
      title={tab.url}
      onClick={() => {
        if (!tab.active) void store.tabsAct('activate', { index: tab.index })
        onOpen()
      }}
    >
      <canvas ref={canvas} className="shot" />
      <span className="desk-pip-cap">
        <b>{tab.title || tab.url.replace(/^https?:\/\//, '') || t('gui.br.tab_blank')}</b>
        {tab.agent ? (
          <span className="desk-pip-tag" title={t('gui.br.by_agent', { n: tab.agent })}>
            {t('gui.br.agent_tag', { n: tab.agent })}
          </span>
        ) : null}
        <s>{tab.url.replace(/^https?:\/\//, '')}</s>
      </span>
    </button>
  )
}

export function BrowserPip({ onOpen, empty }: { onOpen: () => void; empty: ReactNode }): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.getState)

  useEffect(() => {
    store.setPip(true)
    if (store.getState().avail === null) void store.poll(true)
    return () => {
      store.setPip(false)
      store.hidden()
      /* The strip is a shared timer: stopping it on the way out would leave a
         window that is still open watching a tab list that stopped moving. */
      if (!store.showing()) store.tabTick(false)
    }
  }, [])

  /* Hold the lease while a page streams; while there is none, or the browser
     is popped out, poll cheaply for the moment one appears -- the poll asks
     `browser.state` in the popped-out case, so no window is captured for a
     thumbnail that shows a note instead. */
  const live = s.started && !s.headful
  useEffect(() => {
    if (live) {
      void store.watch(true)
      void store.tabsSync()
      store.tabTick(true)
    } else store.tick(true)
  }, [live])

  if (!s.started) return <>{empty}</>
  if (s.headful) {
    return (
      <button type="button" className="desk-pip" title={s.url} onClick={onOpen}>
        <span className="desk-pip-note">{t('gui.br.popped')}</span>
        <span className="desk-pip-cap">
          <b>{s.title || s.url}</b>
          <s>{s.url}</s>
        </span>
      </button>
    )
  }
  /* Before the first tab list lands there is still a page, and the card for it
     is what the reader came to see. */
  const rows: BrowserTabRow[] = s.tabs.length ? s.tabs : [{ index: 0, url: s.url, title: s.title, active: true }]
  return (
    <div className="desk-pips">
      {rows.map((tab) => (
        <PipCard key={tab.index} tab={tab} onOpen={onOpen} />
      ))}
    </div>
  )
}
