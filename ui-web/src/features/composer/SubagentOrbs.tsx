/* The running subagents on the waiting row: overlapping orbs, as many as the
   row has room for, the rest folded into a +N chip. An orb that finishes
   stays in place for its leave transition before it is dropped, and one that
   starts or comes out of the fold slides in, so the stack never jumps. */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactElement } from 'react'

import { AgentMark } from '../../components/AgentMark'
import { t } from '../../i18n/t'
import { formatDuration } from '../../lib/duration'

import type { SubagentOrb } from './store'

/* page.css's geometry for `.cp-orbs` (--d, --ov and the hover spread) and
   `.cp-more`: an orb's diameter, how far each overlaps the one before it,
   how far the hover spread opens, and the fold chip's width. */
const D = 30
const OVERLAP = 9
const SPREAD = -4.5
const CHIP = D + 7.5
/* page.css's `.cp-orb` width/opacity transition; the leaving orb is dropped
   once it has collapsed. */
const LEAVE_MS = 280

type Phase = 'enter' | 'stay' | 'leave'
interface Slot { orb: SubagentOrb; phase: Phase }

/* The next stack from the previous one and the orbs to show: shown orbs in
   their own order, each leaving orb kept just after the neighbour it sat
   behind. A key that comes back while leaving is the same orb again. */

/** How many orbs to draw in `room` px: n orbs take 9 + 21n, so all of them
 *  while that fits, otherwise as many as leave room for the +N chip (28.5 px
 *  more), and never fewer than one. */
export function orbsThatFit(total: number, room: number): number {
  const step = D - OVERLAP
  if (OVERLAP + step * total <= room) return total
  return Math.min(total, Math.max(1, Math.floor((room - CHIP) / step)))
}

/** The overlap the hover spread opens to: the full spread where it fits,
 *  otherwise only as far as `room` allows, so a stack that just fits at rest
 *  does not spill past the row when hovered. */
export function openOverlap(count: number, folded: boolean, room: number): number {
  const gaps = count - 1 + (folded ? 1 : 0)
  if (gaps <= 0) return SPREAD
  const needed = (D * count + (folded ? CHIP : 0) - room) / gaps
  return Math.min(OVERLAP, Math.max(SPREAD, needed))
}

function mergeSlots(prev: readonly Slot[], shown: readonly SubagentOrb[]): Slot[] {
  const keys = new Set(shown.map((o) => o.key))
  const before = new Map(prev.map((s) => [s.orb.key, s]))
  const out: Slot[] = shown.map((orb) => {
    const was = before.get(orb.key)
    return { orb, phase: was === undefined ? 'enter' : was.phase === 'enter' ? 'enter' : 'stay' }
  })
  prev.forEach((slot, i) => {
    if (keys.has(slot.orb.key)) return
    /* Still in its first, collapsed frame: it was never seen, so it goes
       without a leave -- the first measurement folds orbs exactly so. */
    if (slot.phase === 'enter') return
    let at = 0
    for (let j = i - 1; j >= 0; j--) {
      const anchor = out.findIndex((s) => s.orb.key === prev[j]!.orb.key)
      if (anchor >= 0) { at = anchor + 1; break }
    }
    out.splice(at, 0, { orb: slot.orb, phase: 'leave' })
  })
  return out
}

const tipOf = (orb: SubagentOrb, now: number): string =>
  orb.startedAt === null ? orb.name : `${orb.name} · ${formatDuration(now - orb.startedAt)}`

export function SubagentOrbs({ orbs }: { orbs: readonly SubagentOrb[] }): ReactElement | null {
  /* From the stack's left edge to the right edge of the box that holds the
     row. Unmeasured, every orb is offered; the first measurement, before
     paint, folds what does not fit, and a later resize of that box measures
     again -- which is how the desk changes it: the panel reserves width on the
     transcript column (`.scroll > .col`), narrowing and sliding that box
     rather than this row's own wrapper, and the wrapper the host mounts is
     `display: contents`, so the nearest ancestor with a box is the one to
     watch. */
  const [room, setRoom] = useState(Number.POSITIVE_INFINITY)
  const watch = useRef<ResizeObserver | null>(null)
  const attach = useCallback((el: HTMLSpanElement | null) => {
    watch.current?.disconnect()
    watch.current = null
    if (!el) return
    let host = el.parentElement
    while (host && host !== document.documentElement && host.getBoundingClientRect().width === 0) host = host.parentElement
    if (!host) return
    const measure = (): void => setRoom(host.getBoundingClientRect().right - el.getBoundingClientRect().left)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    watch.current = new ResizeObserver(measure)
    watch.current.observe(host)
  }, [])
  useEffect(() => () => watch.current?.disconnect(), [])
  const fit = orbsThatFit(orbs.length, room)
  const shown = useMemo(() => orbs.slice(0, fit), [orbs, fit])
  const [slots, setSlots] = useState<Slot[]>(() => shown.map((orb) => ({ orb, phase: 'enter' })))
  const [now, setNow] = useState(() => Date.now())
  const active = slots.length > 0

  /* The hover label's elapsed time. Once the host turn ends nothing else
     repaints the waiting row, so the stack keeps its own second hand, and
     only while it has an orb to label. */
  useEffect(() => {
    if (!active) return undefined
    const clock = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(clock)
  }, [active])
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>())

  /* Before paint, so a pushed list never shows one frame of the old stack. */
  useLayoutEffect(() => { setSlots((prev) => mergeSlots(prev, shown)) }, [shown])

  useEffect(() => {
    const leaving = new Set(slots.filter((s) => s.phase === 'leave').map((s) => s.orb.key))
    for (const [key, timer] of timers.current) {
      if (!leaving.has(key)) { clearTimeout(timer); timers.current.delete(key) }
    }
    for (const key of leaving) {
      if (timers.current.has(key)) continue
      timers.current.set(key, setTimeout(() => {
        timers.current.delete(key)
        setSlots((prev) => prev.filter((s) => !(s.orb.key === key && s.phase === 'leave')))
      }, LEAVE_MS))
    }
    if (!slots.some((s) => s.phase === 'enter')) return undefined
    /* One frame in the collapsed state, then the transition to full size. */
    const frame = requestAnimationFrame(() => {
      setSlots((prev) => prev.map((s) => (s.phase === 'enter' ? { ...s, phase: 'stay' } : s)))
    })
    return () => cancelAnimationFrame(frame)
  }, [slots])

  useEffect(() => {
    const pending = timers.current
    return () => { for (const timer of pending.values()) clearTimeout(timer); pending.clear() }
  }, [])

  const more = orbs.length - fit
  if (!slots.length && !more) return null
  const names = orbs.map((o) => o.name).join(', ')
  const label = orbs.length === 1
    ? t('gui.live.orbs_label_one', { names })
    : t('gui.live.orbs_label_n', { n: orbs.length, names })
  const open = { '--ov-open': `${Math.round(openOverlap(fit, more > 0, room) * 10) / 10}px` } as CSSProperties
  return (
    <span className="cp-orbs" role="img" aria-label={label} ref={attach} style={open}>
      {slots.map(({ orb, phase }) => (
        <span
          key={orb.key}
          className="cp-orb"
          data-key={orb.key}
          data-tip={tipOf(orb, now)}
          data-enter={phase === 'enter' ? '' : undefined}
          data-leave={phase === 'leave' ? '' : undefined}
        >
          <span className="cp-ball"><AgentMark preset={orb.preset} own={orb.own} agent={orb.name} /></span>
        </span>
      ))}
      {more ? (
        <span className="cp-orb cp-more"><span className="cp-ball"><span>{`+${more}`}</span></span></span>
      ) : null}
    </span>
  )
}
