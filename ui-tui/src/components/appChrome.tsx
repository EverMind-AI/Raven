// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { Box, type ScrollBoxHandle, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { type ReactNode, type RefObject, useEffect, useMemo, useRef, useState } from 'react'
import unicodeSpinners from 'unicode-animations'

import type { IndicatorStyle } from '../app/interfaces.js'
import type { Theme } from '../theme.js'
import type { Msg, Usage } from '../types.js'

import { $delegationState } from '../app/delegationStore.js'
import { $liveAgents, liveAgentCounts } from '../app/liveAgentsStore.js'
import { useTurnSelector } from '../app/turnStore.js'
import { $uiState } from '../app/uiStore.js'
import { FACES } from '../content/faces.js'
import { VERBS } from '../content/verbs.js'
import { fmtDuration } from '../domain/messages.js'
import { stickyPromptFromViewport } from '../domain/viewport.js'
import { hasMeaningfulReasoning } from '../lib/reasoning.js'
import { buildSubagentTree, treeTotals } from '../lib/subagentTree.js'
import { fmtK } from '../lib/text.js'
import { useScrollbarSnapshot, useViewportSnapshot } from '../lib/viewportStore.js'

const FACE_TICK_MS = 2500
const HEART_COLORS = ['#ff5fa2', '#ff4d6d']

// Keep verb segment width stable so status-bar content to the right doesn't
// jitter when the ticker rotates between short/long verbs.
export const VERB_PAD_LEN = VERBS.reduce((max, v) => Math.max(max, v.length), 0) + 1 // + ellipsis
export const padVerb = (verb: string) => `${verb}…`.padEnd(VERB_PAD_LEN, ' ')

// Compact alternates for the `emoji` and `ascii` indicator styles.
const emojiFrames = (brandMark: string) => [`${brandMark} `, '🌀', '🤔', '✨', '🍵', '🔮']
const ASCII_FRAMES = ['|', '/', '-', '\\']

// Faster tick for spinner-style indicators — they read as motion only
// at frame rates closer to their authored interval.
const SPINNER_TICK_MS = 100

interface IndicatorRender {
  frame: string
  intervalMs: number
  // When false, FaceTicker hides the rotating verb and just shows the
  // glyph + duration.  Lets `unicode` stay minimal while the other
  // styles keep the verb-rotation flavour users associate with the
  // running… status.
  showVerb: boolean
}

const renderIndicator = (style: IndicatorStyle, tick: number, brandMark: string): IndicatorRender => {
  if (style === 'kaomoji') {
    return { frame: FACES[tick % FACES.length] ?? '', intervalMs: FACE_TICK_MS, showVerb: true }
  }

  if (style === 'emoji') {
    const frames = emojiFrames(brandMark)

    return {
      frame: frames[tick % frames.length] ?? `${brandMark} `,
      intervalMs: SPINNER_TICK_MS * 6,
      showVerb: true
    }
  }

  if (style === 'ascii') {
    return {
      frame: ASCII_FRAMES[tick % ASCII_FRAMES.length] ?? '|',
      intervalMs: SPINNER_TICK_MS,
      showVerb: true
    }
  }

  // 'unicode' — braille spinner (fixed 1-col).  Authored interval is
  // ~80ms; honour it but bound below at a safe minimum so React
  // re-renders stay reasonable.  This style is for users who want
  // the cleanest possible status, so no verb rotation either.
  const spinner = unicodeSpinners.braille
  const frame = spinner.frames[tick % spinner.frames.length] ?? '⠋'

  return { frame, intervalMs: Math.max(SPINNER_TICK_MS, spinner.interval), showVerb: false }
}

export function FaceTicker({ color, startedAt }: { color: string; startedAt?: null | number }) {
  const ui = useStore($uiState)
  const style = ui.indicatorStyle
  const [tick, setTick] = useState(() => Math.floor(Math.random() * 1000))
  const [verbTick, setVerbTick] = useState(() => Math.floor(Math.random() * VERBS.length))
  const [now, setNow] = useState(() => Date.now())

  // Pre-compute cadence + verb-visibility for the active style so an
  // `/indicator` switch re-arms the interval (and skips the verb timer
  // for verb-less styles like `unicode`) without leaving the previous
  // timer dangling.
  const brandMark = ui.theme.brand.icon
  const { intervalMs, showVerb } = renderIndicator(style, 0, brandMark)

  useEffect(() => {
    const glyph = setInterval(() => setTick(n => n + 1), intervalMs)
    const clock = setInterval(() => setNow(Date.now()), 1000)
    // Verb timer is gated on `showVerb` — `unicode` style hides the verb
    // entirely, so cycling `verbTick` would be an avoidable re-render.
    const verb = showVerb ? setInterval(() => setVerbTick(n => n + 1), FACE_TICK_MS) : null

    return () => {
      clearInterval(glyph)
      clearInterval(clock)

      if (verb !== null) {
        clearInterval(verb)
      }
    }
  }, [brandMark, intervalMs, showVerb])

  const { frame } = renderIndicator(style, tick, brandMark)
  const verb = VERBS[verbTick % VERBS.length] ?? ''
  const verbSegment = showVerb ? ` ${padVerb(verb)}` : ''
  // Leading space keeps a gap between the frame and the duration when the
  // verb segment is hidden (e.g. `unicode` spinner style).  When the verb
  // IS shown, its trailing padding already provides the gap, so the extra
  // space is harmless.
  const durationSegment = startedAt ? ` · ${fmtDuration(now - startedAt)}` : ''

  return (
    <Text color={color}>
      {frame}
      {verbSegment}
      {durationSegment}
    </Text>
  )
}

/**
 * The turn's live indicator, rendered at the tail of the transcript rather than
 * in the status rule: it marks the spot the reply is about to land in, which is
 * what a reader watching for output is actually looking at.
 *
 * It shows only while the turn has nothing else to show for itself -- after the
 * prompt and before the first token, and again between a tool result and the
 * text that follows it. Once `streaming` has content the reply itself is the
 * progress, a running tool row carries its own spinner and elapsed, and a
 * reasoning row carries both as well while it scrolls its own live tail; any of
 * the three stands in for this and it steps aside. Idle turns render nothing,
 * so the transcript gains and loses exactly one row.
 */
export function WorkingIndicator({
  busy,
  color,
  startedAt
}: {
  busy: boolean
  color: string
  startedAt?: null | number
}) {
  const awaitingReply = useTurnSelector(
    state => !state.streaming && state.tools.length === 0 && !hasMeaningfulReasoning(state.reasoning)
  )

  if (!busy || !awaitingReply) {
    return null
  }

  return (
    <Box height={1}>
      <FaceTicker color={color} startedAt={startedAt} />
    </Box>
  )
}

function ctxBarColor(pct: number | undefined, t: Theme) {
  if (pct == null) {
    return t.color.muted
  }

  if (pct >= 95) {
    return t.color.statusCritical
  }

  if (pct > 80) {
    return t.color.statusBad
  }

  if (pct >= 50) {
    return t.color.statusWarn
  }

  return t.color.statusGood
}

function ctxBar(pct: number | undefined, w = 10) {
  const p = Math.max(0, Math.min(100, pct ?? 0))
  const filled = Math.round((p / 100) * w)

  const [filledChar, emptyChar] = '█░'
  // const [filledChar, emptyChar] = '▰▱'

  return filledChar.repeat(filled) + emptyChar.repeat(w - filled)
}

function SpawnHud({ t }: { t: Theme }) {
  // Tight HUD that only appears when the session is actually fanning out.
  // Colour escalates to warn/error as depth or concurrency approaches the cap.
  // Live counts come from `$liveAgents` (spawns and dag nodes, cross-turn);
  // the turn tree still contributes depth when a gateway streams one.
  const delegation = useStore($delegationState)
  const liveRows = useStore($liveAgents)
  const subagents = useTurnSelector(state => state.subagents)

  const tree = useMemo(() => buildSubagentTree(subagents), [subagents])
  const totals = useMemo(() => treeTotals(tree), [tree])
  const { pending, running } = liveAgentCounts(liveRows)

  if (!totals.descendantCount && !delegation.paused && running === 0 && pending === 0) {
    return null
  }

  const maxDepth = delegation.maxSpawnDepth
  const maxConc = delegation.maxConcurrentChildren
  const depth = Math.max(0, totals.maxDepthFromHere)

  const depthRatio = maxDepth ? depth / maxDepth : 0
  const concRatio = maxConc ? running / maxConc : 0
  const ratio = Math.max(depthRatio, concRatio)

  const color = delegation.paused || ratio >= 1 ? t.color.error : ratio >= 0.66 ? t.color.warn : t.color.muted

  const pieces: string[] = []

  if (delegation.paused) {
    pieces.push('⏸ paused')
  }

  if (totals.descendantCount > 0) {
    const depthLabel = maxDepth ? `${depth}/${maxDepth}` : `${depth}`
    pieces.push(`d${depthLabel}`)
  }

  if (running > 0 || pending > 0) {
    // `running/cap` is what drives the warn colour; `+N○` is what is still
    // queued behind the gate.
    const widthLabel = maxConc ? `${running}/${maxConc}` : `${running}`
    const suffix = pending > 0 ? `+${pending}○` : ''
    pieces.push(`⚡${widthLabel}${suffix}`)
  }

  const atCap = depthRatio >= 1 || concRatio >= 1

  return (
    <Text color={color}>
      {atCap ? ' │ ⚠ ' : ' │ '}
      {pieces.join(' ')}
      {running > 0 || pending > 0 ? <Text color={t.color.label}> ^T</Text> : null}
    </Text>
  )
}

function SessionDuration({ startedAt }: { startedAt: number }) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 1000)

    return () => clearInterval(id)
  }, [startedAt])

  return fmtDuration(now - startedAt)
}

const effortLabel = (effort?: string) => {
  const value = String(effort ?? '')
    .trim()
    .toLowerCase()

  return value && value !== 'medium' && value !== 'normal' && value !== 'default' ? value : ''
}

const shortModelLabel = (model: string) =>
  model
    .split('/')
    .pop()!
    .replace(/^claude[-_]/, '')
    .replace(/^anthropic[-_]/, '')
    .replace(/[-_]/g, ' ')
    .replace(/\b(\d+)\s+(\d+)\b/g, '$1.$2')
    .trim()

const modelLabel = (model: string, effort?: string, fast?: boolean) =>
  [shortModelLabel(model), effortLabel(effort), fast ? 'fast' : ''].filter(Boolean).join(' ')

export function GoodVibesHeart({ tick, t }: { tick: number; t: Theme }) {
  const [active, setActive] = useState(false)
  const [color, setColor] = useState(t.color.accent)

  useEffect(() => {
    if (tick <= 0) {
      return
    }

    const palette = [t.color.error, t.color.warn, t.color.accent]
    setColor(palette[Math.floor(Math.random() * palette.length)]!)
    setActive(true)

    const id = setTimeout(() => setActive(false), 650)

    return () => clearTimeout(id)
  }, [t.color.accent, tick])

  if (!active) {
    return null
  }

  return <Text color={color}>♥</Text>
}

export function StatusRule({
  cwdLabel,
  cols,
  status,
  statusColor,
  model,
  modelFast,
  modelReasoningEffort,
  usage,
  bgCount,
  sessionStartedAt,
  showCost,
  updateAvailable,
  updateCommand,
  t
}: StatusRuleProps) {
  const pct = usage.context_percent
  const barColor = ctxBarColor(pct, t)

  const ctxLabel = usage.context_max
    ? `${fmtK(usage.context_used ?? 0)}/${fmtK(usage.context_max)}`
    : usage.total > 0
      ? `${fmtK(usage.total)} tok`
      : ''

  const bar = usage.context_max ? ctxBar(pct) : ''
  // When an update is available, the bottom-right slot shows the upgrade nudge
  // in place of the cwd/branch label (dynamic, no extra line).
  const rightLabel = updateAvailable ? `↑ Update available — run ${updateCommand || 'raven upgrade'}` : cwdLabel
  const leftWidth = Math.max(12, cols - rightLabel.length - 3)

  return (
    <Box height={1}>
      <Box flexShrink={1} width={leftWidth}>
        <Text color={t.color.border} wrap="truncate-end">
          {'─ '}
          <Text color={statusColor}>{`● ${status} `}</Text>
          <Text color={t.color.muted}> {modelLabel(model, modelReasoningEffort, modelFast)}</Text>
          {ctxLabel ? <Text color={t.color.muted}> {ctxLabel}</Text> : null}
          {bar ? (
            <Text color={t.color.muted}>
              {'   '}
              <Text color={barColor}>[{bar}]</Text> <Text color={barColor}>{pct != null ? `${pct}%` : ''}</Text>
            </Text>
          ) : null}
          {sessionStartedAt ? (
            <Text color={t.color.muted}>
              {'   '}
              <SessionDuration startedAt={sessionStartedAt} />
            </Text>
          ) : null}
          {typeof usage.compressions === 'number' && usage.compressions > 0 ? (
            <Text color={t.color.muted}>
              {'   '}
              <Text
                color={
                  usage.compressions >= 10 ? t.color.error : usage.compressions >= 5 ? t.color.warn : t.color.muted
                }
              >
                cmp {usage.compressions}
              </Text>
            </Text>
          ) : null}
          <SpawnHud t={t} />
          {bgCount > 0 ? <Text color={t.color.muted}> {bgCount} bg</Text> : null}
          {showCost && typeof usage.cost_usd === 'number' ? (
            <Text color={t.color.muted}> ${usage.cost_usd.toFixed(4)}</Text>
          ) : null}
        </Text>
      </Box>

      <Text color={t.color.border}> ─ </Text>
      <Text color={updateAvailable ? t.color.warn : t.color.label}>{rightLabel}</Text>
    </Box>
  )
}

export function FloatBox({ children, color }: { children: ReactNode; color: string }) {
  return (
    <Box
      alignSelf="flex-start"
      borderColor={color}
      borderStyle="round"
      flexDirection="column"
      marginTop={1}
      opaque
      paddingX={1}
    >
      {children}
    </Box>
  )
}

export function StickyPromptTracker({ messages, offsets, scrollRef, onChange }: StickyPromptTrackerProps) {
  const { atBottom, bottom, top } = useViewportSnapshot(scrollRef)
  const text = stickyPromptFromViewport(messages, offsets, top, bottom, atBottom)

  useEffect(() => onChange(text), [onChange, text])

  return null
}

export function TranscriptScrollbar({ scrollRef, t }: TranscriptScrollbarProps) {
  const [hover, setHover] = useState(false)
  const [grab, setGrab] = useState<number | null>(null)
  const grabRef = useRef<number | null>(null)
  const { scrollHeight: total, top: pos, viewportHeight: vp } = useScrollbarSnapshot(scrollRef)

  if (!vp) {
    return <Box width={1} />
  }

  const s = scrollRef.current
  const scrollable = total > vp
  const thumb = scrollable ? Math.max(1, Math.round((vp * vp) / total)) : vp
  const travel = Math.max(1, vp - thumb)
  const thumbTop = scrollable ? Math.round((pos / Math.max(1, total - vp)) * travel) : 0
  const thumbColor = grab !== null ? t.color.primary : hover ? t.color.accent : t.color.border
  const trackColor = hover ? t.color.border : t.color.muted

  const jump = (row: number, offset: number) => {
    if (!s || !scrollable) {
      return
    }

    s.scrollTo(Math.round((Math.max(0, Math.min(travel, row - offset)) / travel) * Math.max(0, total - vp)))
  }

  return (
    <Box
      flexDirection="column"
      onMouseDown={(e: { localRow?: number }) => {
        const row = Math.max(0, Math.min(vp - 1, e.localRow ?? 0))
        const off = row >= thumbTop && row < thumbTop + thumb ? row - thumbTop : Math.floor(thumb / 2)

        grabRef.current = off
        setGrab(off)
        jump(row, off)
      }}
      onMouseDrag={(e: { localRow?: number }) =>
        jump(Math.max(0, Math.min(vp - 1, e.localRow ?? 0)), grabRef.current ?? Math.floor(thumb / 2))
      }
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onMouseUp={() => {
        grabRef.current = null
        setGrab(null)
      }}
      width={1}
    >
      {!scrollable ? (
        <Text color={trackColor} dim>
          {' \n'.repeat(Math.max(0, vp - 1))}{' '}
        </Text>
      ) : (
        <>
          {thumbTop > 0 ? (
            <Text color={trackColor} dim={!hover}>
              {`${'│\n'.repeat(Math.max(0, thumbTop - 1))}${thumbTop > 0 ? '│' : ''}`}
            </Text>
          ) : null}
          {thumb > 0 ? (
            <Text color={thumbColor}>{`${'┃\n'.repeat(Math.max(0, thumb - 1))}${thumb > 0 ? '┃' : ''}`}</Text>
          ) : null}
          {vp - thumbTop - thumb > 0 ? (
            <Text color={trackColor} dim={!hover}>
              {`${'│\n'.repeat(Math.max(0, vp - thumbTop - thumb - 1))}${vp - thumbTop - thumb > 0 ? '│' : ''}`}
            </Text>
          ) : null}
        </>
      )}
    </Box>
  )
}

interface StatusRuleProps {
  bgCount: number
  cols: number
  cwdLabel: string
  model: string
  modelFast?: boolean
  modelReasoningEffort?: string
  sessionStartedAt?: null | number
  showCost: boolean
  status: string
  statusColor: string
  t: Theme
  updateAvailable?: boolean
  updateCommand?: string
  usage: Usage
}

interface StickyPromptTrackerProps {
  messages: readonly Msg[]
  offsets: ArrayLike<number>
  onChange: (text: string) => void
  scrollRef: RefObject<ScrollBoxHandle | null>
}

interface TranscriptScrollbarProps {
  scrollRef: RefObject<ScrollBoxHandle | null>
  t: Theme
}
