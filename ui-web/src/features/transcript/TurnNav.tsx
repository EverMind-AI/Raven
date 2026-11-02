/* The turn nav: a strip of marks beside the transcript, one per turn, that
 * says where the reader is in a long conversation and takes them to any turn.
 *
 * A mark is a turn's opening question -- an `ask` segment in the
 * conversation's lane -- and nothing else: a message merged into a turn that
 * was already running opens no turn (`AskData.midTurn`), so it gets no mark.
 * Pointing at a mark raises a card with the turn's number, how long ago it was
 * asked and what was asked; a click brings the question to the top of the
 * scroller.
 *
 * Mounted once, by the domain's root (TranscriptApp.tsx) in `#turnNav`, the
 * box src/chrome/ChatTop.tsx renders beside the scroller and hands over empty,
 * rather than drawn by a lane: a lane held off the stage keeps its root, so a
 * nav drawn per lane would draw one for every conversation the reader left
 * mid-turn. It reads the one lane on the stage instead (`shownLane`) and
 * watches the stage for the host swaps the session layer makes as the reader
 * moves between conversations.
 */
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import { ago } from '../../lib/ago'
import { HEADER_LEAD, headerBand } from '../../lib/dom'
import { shownLane } from './mount'
import * as store from './store'
import * as tail from './tail'

import type { AskData, Lane, Seg } from './types'
import type { JSX } from 'react'

/* The reading line, as a fraction of the height the header leaves visible:
   the last question scrolled past it is the turn being read. Below the band,
   so a question that has just come into view counts as reached before it is
   under the header. */
const READ_LINE = 0.35
/* How much of a question a mark's accessible name carries. */
const NAME_MAX = 200

/** The questions that opened a turn, in the order the lane holds them. */
export function turnsOf(segs: readonly Seg[]): AskData[] {
  return segs.filter((s): s is AskData => s.kind === 'ask' && !s.midTurn)
}

/** Which turn is being read: the last whose question is at or above `line`,
 *  the first while none is, and the last once the reader is at the tail --
 *  where a short final turn may never reach the line at all. */
export function readingTurn(tops: readonly number[], line: number, atEnd: boolean): number {
  if (!tops.length) return -1
  if (atEnd) return tops.length - 1
  let at = 0
  tops.forEach((top, i) => {
    if (top <= line) at = i
  })
  return at
}

/* What a turn was about, on one line: the question; for a turn nobody typed,
   what set it off; for a message that was only files, their names. */
function saidOf(seg: AskData): string {
  const body = seg.body.replace(/\s+/g, ' ').trim()
  if (body) return body
  if (seg.auto) return t('gui.deleg.by_' + seg.auto.origin, undefined, seg.auto.origin)
  return seg.atts.map((p) => String(p).split('/').pop() || String(p)).join(', ')
}

const questionOf = (seg: AskData): HTMLElement | null =>
  document.querySelector<HTMLElement>(`#stage [data-ask="${seg.id}"]`)

/* The reader chose a place, so the transcript stops following its tail before
   it moves; the scroller's own listener turns tail-follow back on if the move
   ends at the tail. */
function goTo(seg: AskData): void {
  const scroll = document.getElementById('scroll')
  const question = questionOf(seg)
  if (!scroll || !question) return
  tail.setStuck(false)
  /* The header floats over the scroller, so the question is put under the
     title bar's lower edge rather than at the scroller's own top (lib/dom.ts,
     headerBand), with the same lead the up pill leaves. */
  const top = scroll.scrollTop + question.getBoundingClientRect().top - scroll.getBoundingClientRect().top
    - headerBand(scroll) - HEADER_LEAD
  scroll.scrollTo({ top: Math.max(0, top) })
}

export function TurnNav(): JSX.Element | null {
  const [lane, setLane] = useState<Lane | null>(shownLane)
  const listen = useCallback((cb: () => void) => (lane ? store.subscribe(lane, cb) : () => {}), [lane])
  const listV = useSyncExternalStore(listen, () => (lane ? lane.listV : -1))
  const [reading, setReading] = useState(-1)
  /* Keyed by the question rather than its index, so a turn arriving while the
     card is up does not move the card onto another turn. */
  const [tip, setTip] = useState<{ id: number; y: number } | null>(null)
  const navRef = useRef<HTMLElement | null>(null)
  const listRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const stage = document.getElementById('stage')
    if (!stage) return
    const follow = (): void => setLane(shownLane())
    follow()
    const watch = new MutationObserver(follow)
    watch.observe(stage, { childList: true })
    return () => watch.disconnect()
  }, [])

  /* Measured a frame after the scroll rather than on it, and again whenever
     the list changes, since a new turn moves the tail. */
  useEffect(() => {
    const scroll = document.getElementById('scroll')
    if (!scroll || !lane) return
    const asks = turnsOf(lane.segs)
    let frame = 0
    const measure = (): void => {
      frame = 0
      const box = scroll.getBoundingClientRect()
      const band = headerBand(scroll)
      const tops = asks.map((seg) => questionOf(seg)?.getBoundingClientRect().top ?? Infinity)
      const atEnd = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 4
      setReading(readingTurn(tops, box.top + band + (box.height - band) * READ_LINE, atEnd))
    }
    const soon = (): void => {
      if (!frame) frame = requestAnimationFrame(measure)
    }
    soon()
    scroll.addEventListener('scroll', soon, { passive: true })
    return () => {
      scroll.removeEventListener('scroll', soon)
      if (frame) cancelAnimationFrame(frame)
    }
  }, [lane, listV])

  /* A conversation with more turns than the strip has room for scrolls the
     strip, centred on the mark being read: merely in view would leave it in
     the fade at either end (styles.css), which is where the reader's place
     would look least marked. Clamped, so the ends stop at the edge. */
  useEffect(() => {
    const list = listRef.current
    const mark = list?.children[reading] as HTMLElement | undefined
    if (!list || !mark) return
    const centred = mark.offsetTop + mark.offsetHeight / 2 - list.clientHeight / 2
    list.scrollTop = Math.max(0, Math.min(centred, list.scrollHeight - list.clientHeight))
  }, [reading])

  const turns = lane ? turnsOf(lane.segs) : []
  /* One turn is nowhere to go. */
  if (turns.length < 2) return null
  const point = (seg: AskData, mark: HTMLElement): void => {
    const nav = navRef.current
    const r = mark.getBoundingClientRect()
    setTip({ id: seg.id, y: nav ? r.top + r.height / 2 - nav.getBoundingClientRect().top : 0 })
  }
  const label = (i: number): string => t('gui.transcript.nav_turn', { n: i + 1 })
  const shown = tip ? turns.findIndex((seg) => seg.id === tip.id) : -1
  const said = shown >= 0 ? (turns[shown] as AskData) : null
  return (
    <nav className="transcript-nav" ref={navRef} aria-label={t('gui.transcript.nav')}>
      <div className="transcript-navlist" ref={listRef} data-thumb="none">
        {turns.map((seg, i) => {
          const what = saidOf(seg)
          return (
            <button
              key={seg.id}
              className="transcript-navmark"
              aria-label={`${label(i)}: ${what.length > NAME_MAX ? `${what.slice(0, NAME_MAX)}...` : what}`}
              aria-current={i === reading ? 'true' : undefined}
              onMouseEnter={(e) => point(seg, e.currentTarget)}
              onFocus={(e) => point(seg, e.currentTarget)}
              onMouseLeave={() => setTip(null)}
              onBlur={() => setTip(null)}
              onClick={() => goTo(seg)}
            >
              <i />
            </button>
          )
        })}
      </div>
      {said && tip ? (
        <div className="transcript-navtip" style={{ top: tip.y }} aria-hidden="true">
          <span className="transcript-navwhen">{[label(shown), ago(said.at)].filter(Boolean).join(' · ')}</span>
          <span className="transcript-navsaid">{saidOf(said)}</span>
        </div>
      ) : null}
    </nav>
  )
}
