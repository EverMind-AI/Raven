// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { activeColorTier, Box, NoSelect, stringWidth, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { memo, type ReactNode, useEffect, useMemo, useState } from 'react'

import type { Segment } from '../domain/episodeSummary.js'
import type { Theme } from '../theme.js'
import type { Episode, EpisodeTool, Msg } from '../types.js'

import { $directChat, viewKeyOf } from '../app/directChatStore.js'
import { $folds, toggleFold } from '../app/foldStore.js'
import {
  callFailed,
  failureNote,
  previewLines,
  segmentTurn,
  TOOL_PREVIEW_ROWS,
  toolArgument,
  toolParts,
  toolsSummary,
  totalDurationMs
} from '../domain/episodeSummary.js'
import { fmtDuration } from '../domain/messages.js'
import { hasMeaningfulReasoning } from '../lib/reasoning.js'
import { boundedLiveRenderText, compactPreview, tailPreview, clipToWidthFromEnd } from '../lib/text.js'
import { DagPanel } from './dagPanel.js'
import { Md } from './markdown.js'
import { StreamingMd } from './streamingMarkdown.js'
import { Spinner } from './thinking.js'

// Everything the transcript renders sits one step in from the edge, which is
// also where a normal assistant message's body starts (the width messageLine
// reserves for its gutter). Prose spends that step on the same reply marker;
// activity leaves it blank. Either way the body column is shared on purpose:
// inventing a second left edge would misalign this message kind from every
// other one. Depth steps in from there.
const INDENT = 2
const STEP = 2
const MIN_TAIL = 12

// A detail block paints a background so the argument and its output read as one
// object rather than more transcript rows. Below 256 colors there is no shade
// between "black" and "grey" that stays subtle, so those terminals get a rule
// instead of a fill.
const canFill = () => activeColorTier() >= 2

// Re-render once a second while `active`, so an in-flight call can show how long
// it has been running. Idle turns install no timer.
const useNow = (active: boolean) => {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!active) {
      return
    }

    const id = setInterval(() => setNow(Date.now()), 1000)

    return () => clearInterval(id)
  }, [active])

  return now
}

const elapsedOf = (tool: EpisodeTool, now: number): number | undefined =>
  tool.durationMs ?? (!tool.done && tool.startedAt ? Math.max(0, now - tool.startedAt) : undefined)

// fmtDuration floors to whole seconds, so anything quick reads "(0s)" -- a
// column of zeroes that says nothing. Only a call slow enough to have made the
// reader wait gets a time.
const SHOWN_DURATION_MS = 1000
const durationLabel = (ms: number | undefined, running: boolean): string | undefined => {
  if (ms == null || (!running && ms < SHOWN_DURATION_MS)) {
    return undefined
  }

  return `${fmtDuration(ms)}${running ? '…' : ''}`
}

// A row shows a URL without its scheme and a needle inside quotes; both are the
// same argument, so compare them stripped of those.
const sameArgument = (rowDetail: string, argument: string) => {
  const norm = (s: string) =>
    s
      .replace(/^https?:\/\//, '')
      .replace(/\/+$/, '')
      .trim()

  return norm(rowDetail) === norm(argument)
}

// The expanded payload of one call: its full argument, then its output. No tree
// rails inside -- the block's own ground already scopes it, and the two are told
// apart by weight (argument in text color, output dim).
const DetailBlock = memo(function DetailBlock({
  argument,
  compact,
  onToggle,
  output,
  t,
  width
}: {
  argument: string
  compact?: boolean
  onToggle?: () => void
  output: string[]
  t: Theme
  width: number
}) {
  const fill = canFill()
  const body = Math.max(8, width - 2)

  return (
    <Box
      flexDirection="column"
      marginBottom={1}
      onClick={onToggle}
      paddingX={1}
      width={width}
      {...(fill && { backgroundColor: t.color.detailBg })}
    >
      {argument ? (
        <Box>
          {fill ? null : (
            <NoSelect fromLeftEdge>
              <Text color={t.color.border}>{'▏'}</Text>
            </NoSelect>
          )}
          <Box width={body}>
            {/* `wrap` is the only mode this ink measures: wrap-char/wrap-trim
                render wrapped but leave the box one row tall, so the text
                overflowed the block. `wrap` also hard-breaks an unbreakable
                token (a long URL), which is what tool arguments are made of. */}
            <Text color={t.color.text} wrap="wrap">
              {argument}
            </Text>
          </Box>
        </Box>
      ) : null}

      {output.map((line, i) => (
        <Box key={i}>
          {fill ? null : (
            <NoSelect fromLeftEdge>
              <Text color={t.color.border}>{'▏'}</Text>
            </NoSelect>
          )}
          <Box width={body}>
            {/* The block is the one place the whole result appears, so a line
                wraps here rather than ending in an ellipsis -- truncating twice
                (the backend cap, then the column) left nothing readable. */}
            <Text color={t.color.muted} dim wrap="wrap">
              {line}
            </Text>
          </Box>
        </Box>
      ))}
    </Box>
  )
})

// The reasoning row. It is an ActivityRow in spirit but not in layout: the
// spinner sits out in the margin column rather than inline, so the label lands
// on the same column as the block underneath it and as the answer -- one left
// edge for the text, one for the markers.
//
// While the model is still reasoning and the block is closed, the row spends
// whatever columns it has left on a live tail of the reasoning. That is the
// only view of it in that state, and a bare label plus a ticking clock says
// nothing about whether the model is getting anywhere.
const ReasoningRow = memo(function ReasoningRow({
  onToggle,
  running,
  t,
  tail,
  time,
  width
}: {
  onToggle?: () => void
  running?: boolean
  t: Theme
  tail?: string
  time?: string
  width: number
}) {
  const head = `reasoning${time ? ` (${time})` : ''}`
  // ` \u00b7 ` costs 3, and a tail shorter than this is more ellipsis than text.
  const room = width - INDENT - stringWidth(head) - 3
  const trail = tail && room >= MIN_TAIL ? clipToWidthFromEnd(tail, room) : ''

  return (
    <Box width={width}>
      <Box flexShrink={0} width={INDENT}>
        {running ? (
          <NoSelect fromLeftEdge>
            <Text>
              <Spinner color={t.color.accent} variant="tool" />
            </Text>
          </NoSelect>
        ) : null}
      </Box>

      <Box flexGrow={1} minWidth={0} onClick={onToggle}>
        <Text color={t.color.muted} dim wrap="truncate-end">
          {head}
          {trail ? ` \u00b7 ${trail}` : ''}
        </Text>
      </Box>
    </Box>
  )
})

// A left-only hairline. `single` would draw the same U+2502 the tree rails
// used, and this view retired that glyph on purpose; the thinner bar also tells
// an aside apart from a real blockquote inside the answer, which does use
// `single`. Only `left` is ever drawn, so the rest is filler.
const ASIDE_RULE = {
  bottom: ' ',
  bottomLeft: ' ',
  bottomRight: ' ',
  left: '\u258f',
  right: ' ',
  top: ' ',
  topLeft: ' ',
  topRight: ' '
} as const

// The model's scratch work, rendered as an aside instead of as a payload: a
// left rule and muted prose, the same shape a markdown blockquote gets. It
// deliberately does not reuse DetailBlock's filled ground -- that ground reads
// as "tool output" everywhere else in the transcript, and a slab behind ten
// lines of prose outweighs the answer it was only leading up to. The rule is a
// border rather than a glyph in the text so it spans every wrapped row.
//
// Rule and padding together spend exactly INDENT, so the body lands on the
// transcript's own prose column: the reasoning reads as the same column of text
// as the answer under it, with the rule out in the margin the reply marker
// occupies. Indenting the block instead left two ragged left edges.
const ReasoningBlock = memo(function ReasoningBlock({
  onToggle,
  t,
  text,
  width
}: {
  onToggle?: () => void
  t: Theme
  text: string
  width: number
}) {
  const body = Math.max(8, width - 2)

  return (
    <Box
      borderBottom={false}
      borderColor={t.color.muted}
      borderLeft
      borderRight={false}
      borderStyle={ASIDE_RULE}
      borderTop={false}
      marginBottom={1}
      onClick={onToggle}
      paddingLeft={1}
      width={width}
    >
      <Box width={body}>
        {/* `wrap` is the only mode this ink measures: wrap-char/wrap-trim
            render wrapped but leave the box one row tall, so the text
            overflowed the block. No `dim` on top of `muted` -- the rule is
            already doing the separating, and doubling it up made this the
            lowest-contrast text in the UI. */}
        <Text color={t.color.muted} wrap="wrap">
          {text}
        </Text>
      </Box>
    </Box>
  )
})

// One activity row: dim text, an inline duration, and nothing else. There is no
// fold marker -- a summary row announces that it summarizes, and repeating that
// as a glyph on every row is what made the transcript look like a control panel.
// Expandability is a property of the whole activity column, not of each row.
const ActivityRow = memo(function ActivityRow({
  depth,
  failed,
  label,
  note,
  onToggle,
  running,
  t,
  time,
  width
}: {
  depth: number
  failed?: boolean
  label: string
  note?: string
  onToggle?: () => void
  running?: boolean
  t: Theme
  time?: string
  width: number
}) {
  const color = failed ? t.color.error : t.color.muted

  return (
    <Box paddingLeft={depth}>
      {running ? (
        <NoSelect fromLeftEdge>
          <Text>
            <Spinner color={t.color.accent} variant="tool" />{' '}
          </Text>
        </NoSelect>
      ) : null}
      <Box flexGrow={1} minWidth={0} onClick={onToggle}>
        <Text color={color} dim={!failed} wrap="truncate-end">
          {label}
          {note ? ` · ${note}` : ''}
          {time ? ` (${time})` : ''}
        </Text>
      </Box>
    </Box>
  )
})

// A stretch of work between two things the model said. Three depths:
//   folded  -- one row: "listed .raven/, read TOOLS.md, ran 4 commands (2.4s)"
//   open    -- one row per call
//   detail  -- a call's full argument and output, in a filled block
// A single-call stretch skips the middle depth: its folded row already names the
// call, so an identical row underneath would just be the same sentence twice.
// A dag call is the one exception to "folded is one row" -- see dagFor below.
const WorkSegment = memo(function WorkSegment({
  compact,
  defaultOpen,
  isOpen,
  live,
  now,
  openCalls,
  t,
  toggleCall,
  toggleSelf,
  tools,
  width
}: {
  compact?: boolean
  defaultOpen: boolean
  isOpen: boolean
  live: boolean
  now: number
  openCalls: ReadonlySet<string>
  t: Theme
  toggleCall: (id: string) => void
  toggleSelf: () => void
  tools: EpisodeTool[]
  width: number
}) {
  const inFlight = live && tools.some(tool => !tool.done)
  const failure = failureNote(tools)
  const summaryRoom = Math.max(8, width - INDENT)
  const solo = tools.length === 1

  const callRow = (tool: EpisodeTool, depth: number, onToggle: () => void) => {
    const parts = toolParts(tool)
    const running = live && !tool.done
    const failed = callFailed(tool)

    return (
      <ActivityRow
        depth={depth}
        failed={failed}
        key={tool.id}
        label={[parts.verb, parts.detail].filter(Boolean).join(' ')}
        note={failed ? 'failed' : undefined}
        onToggle={onToggle}
        running={running}
        t={t}
        time={durationLabel(elapsedOf(tool, now), running)}
        width={summaryRoom}
      />
    )
  }

  // A dag call's graph is its result, not a detail of it: which node failed is
  // the answer, and no one-line label can carry it. So it renders under the
  // call's own row rather than inside the detail block, at every depth --
  // including a folded summary row. That is exactly why a stretch holding one
  // defaults open, and why folding it back by hand still leaves the graph.
  const dagFor = (tool: EpisodeTool, depth: number) =>
    tool.dag ? (
      <Box key={`g:${tool.id}`} paddingLeft={depth}>
        <DagPanel run={tool.dag} t={t} width={Math.max(24, width - depth)} />
      </Box>
    ) : null

  const detailFor = (tool: EpisodeTool, depth: number, onCollapse: () => void) => {
    const lines = previewLines(tool)
    const shown = lines.length > TOOL_PREVIEW_ROWS ? lines.slice(0, TOOL_PREVIEW_ROWS) : lines
    const hidden = lines.length - shown.length
    const argument = toolArgument(tool)
    const rowDetail = toolParts(tool).detail.replace(/^"|"$/g, '')
    // The block repeats the row only when the row is showing the same thing --
    // the argument modulo a display transform (a stripped scheme, quotes). A
    // containment test is too loose here: `ruff check` is a prefix of `ruff
    // check raven/ ui-tui/` and would have swallowed a real argument.
    const echoed = Boolean(rowDetail) && sameArgument(rowDetail, argument)

    return (
      <Box key={`d:${tool.id}`} paddingLeft={depth}>
        <DetailBlock
          argument={echoed ? '' : argument}
          compact={compact}
          onToggle={onCollapse}
          output={hidden > 0 ? [...shown, `… +${hidden}`] : shown}
          t={t}
          width={Math.max(12, width - depth)}
        />
      </Box>
    )
  }

  // ── Running: the summary so far, plus only the call in hand ──
  if (inFlight) {
    const latest = [...tools].reverse().find(tool => !tool.done) ?? tools[tools.length - 1]!
    const done = totalDurationMs(tools.filter(tool => tool.done))
    const parts = toolParts(latest)
    const elapsed = elapsedOf(latest, now)

    // One call in the whole stretch means the summary IS the call: printing
    // both gives the same sentence twice, under two spinners.
    if (solo) {
      return (
        <Box flexDirection="column">
          <ActivityRow
            depth={INDENT}
            label={[parts.verb, parts.detail].filter(Boolean).join(' ')}
            running
            t={t}
            time={durationLabel(elapsed, true)}
            width={summaryRoom}
          />
          {dagFor(latest, INDENT + STEP)}
        </Box>
      )
    }

    return (
      <Box flexDirection="column">
        <ActivityRow
          depth={INDENT}
          label={toolsSummary(tools, summaryRoom)}
          running
          t={t}
          time={durationLabel(done, false)}
          width={summaryRoom}
        />
        {/* The spinner above already says "in flight"; a second one on the
            call in hand just makes two things twitch at once. */}
        <ActivityRow
          depth={INDENT + STEP}
          label={[parts.verb, parts.detail].filter(Boolean).join(' ')}
          t={t}
          time={durationLabel(elapsed, true)}
          width={summaryRoom}
        />
        {dagFor(latest, INDENT + STEP * 2)}
      </Box>
    )
  }

  const total = totalDurationMs(tools)

  // ── One call: the folded row IS the call; opening goes straight to detail ──
  if (solo) {
    const tool = tools[0]!

    return (
      <Box flexDirection="column">
        {callRow(tool, INDENT, toggleSelf)}
        {dagFor(tool, INDENT + STEP)}
        {isOpen ? detailFor(tool, INDENT, toggleSelf) : null}
      </Box>
    )
  }

  return (
    <Box flexDirection="column">
      <ActivityRow
        depth={INDENT}
        failed={Boolean(failure)}
        label={toolsSummary(tools, summaryRoom)}
        note={failure || undefined}
        onToggle={toggleSelf}
        t={t}
        time={durationLabel(total, false)}
        width={summaryRoom}
      />
      {isOpen
        ? tools.map(tool => (
            <Box flexDirection="column" key={tool.id}>
              {callRow(tool, INDENT + STEP, () => toggleCall(tool.id))}
              {dagFor(tool, INDENT + STEP * 2)}
              {openCalls.has(tool.id) ? detailFor(tool, INDENT + STEP, () => toggleCall(tool.id)) : null}
            </Box>
          ))
        : defaultOpen
          ? tools.map(tool => dagFor(tool, INDENT + STEP))
          : null}
    </Box>
  )
})

// Renders a turn as an alternating stream of what the model said and what the
// machine did. Prose carries the transcript's reply marker in the margin it
// already had; activity shares that margin unmarked, dim, with an inline
// duration. The marker is the same one a plain assistant row draws, so a reply
// reads the same whichever renderer produced it -- previously only prose colour
// and weight separated the two voices here, which left a streaming reply
// unmarked until the turn settled into a history row.
export const EpisodeView = memo(function EpisodeView({
  closedKeys,
  cols,
  compact,
  episodes,
  live = false,
  openKeys,
  scope = '',
  t,
  text
}: {
  closedKeys?: readonly string[]
  cols?: number
  compact?: boolean
  episodes: Episode[]
  live?: boolean
  openKeys?: readonly string[]
  /** Which transcript these folds belong to, so two views never share one. */
  scope?: string
  t: Theme
  text?: string
}) {
  // One fold set: `seg:<id>` a stretch of work, `call:<id>` one call's detail,
  // `rsn:<n>` an episode's chain of thought.
  //
  // Held outside this component (see `foldStore`) so that a row the runtime
  // replaces -- a step landing in a turn still running -- does not close what
  // the reader just opened. `openKeys` and `closedKeys` still seed it, for a
  // caller rendering a fixed state and for the tests.
  const stored = useStore($folds)
  const seeded = useMemo(
    () => ({
      closed: new Set(closedKeys ?? []),
      open: new Set([...(openKeys ?? []), ...(stored[scope]?.open ?? [])])
    }),
    [closedKeys, openKeys, scope, stored]
  )
  const foldOpen = (key: string, defaultOpen: boolean) =>
    seeded.open.has(key)
      ? true
      : seeded.closed.has(key) || (stored[scope]?.closed ?? []).includes(key)
        ? false
        : defaultOpen
  const toggle = (key: string) => toggleFold(scope, key)

  const lastIdx = episodes.length - 1
  const now = useNow(live && episodes.length > 0)
  const width = cols ? Math.max(20, cols - 4) : 116
  const proseWidth = Math.max(20, width - INDENT)
  const liveIndex = live ? episodes[lastIdx]?.index : undefined
  const openCalls = new Set([...seeded.open].filter(k => k.startsWith('call:')).map(k => k.slice(5)))

  // The model's prose carries the same reply marker `messageLine` draws in its
  // gutter (`ROLE.assistant`). Drawn here because this view owns the live turn:
  // without it a reply stayed unmarked until the turn settled into a plain
  // history row, so the marker looked like something the turn earned by
  // finishing. The glyph sits inside the INDENT the prose already had, so the
  // body column does not move and activity rows still line up with it.
  const prose = (node: ReactNode) => (
    <Box>
      <Box flexShrink={0} width={INDENT}>
        <Text color={t.color.muted}>{t.brand.tool}</Text>
      </Box>

      <Box flexDirection="column" width={proseWidth}>
        {node}
      </Box>
    </Box>
  )

  const renderTalk = (ep: Episode) => {
    const reasoning = (ep.reasoning ?? '').trim()
    const hasReasoning = hasMeaningfulReasoning(reasoning)
    const running = ep.index === liveIndex
    // The model streams reasoning and visible content on separate channels; when
    // it splits a sentence across that boundary the narration can begin with a
    // dangling separator ("，那我用…"). Trim leading punctuation/space.
    const narration = (ep.narration ?? '').trim().replace(/^[\s，,、；;：:。.]+/, '')
    const thinking = live && running && !narration && ep.tools.length === 0 && !text
    const reasoningMs =
      ep.reasoningMs ?? ep.durationMs ?? (thinking && ep.startedAt ? Math.max(0, now - ep.startedAt) : undefined)
    // Open by default while the model is still reasoning, but through foldOpen
    // so a reader who closes it mid-run stays closed -- the old expression
    // ignored `closed` outright, which made the row's live tail unreachable.
    const rsnOpen = foldOpen(`rsn:${ep.index}`, thinking && hasReasoning)

    return (
      <Box flexDirection="column" key={`t:${ep.index}`}>
        {hasReasoning ? (
          <>
            <ReasoningRow
              onToggle={() => toggle(`rsn:${ep.index}`)}
              running={thinking}
              t={t}
              tail={thinking && !rsnOpen ? reasoning : ''}
              time={durationLabel(reasoningMs, Boolean(thinking))}
              width={width}
            />
            {rsnOpen ? (
              <ReasoningBlock
                onToggle={() => toggle(`rsn:${ep.index}`)}
                t={t}
                text={(thinking ? tailPreview : compactPreview)(reasoning, 4000)}
                width={width}
              />
            ) : null}
          </>
        ) : null}

        {narration || (live && running && text) ? (
          <Box marginTop={hasReasoning ? 1 : 0}>
            {prose(
              narration ? (
                <Md avail={proseWidth} compact={compact} t={t} text={narration} />
              ) : (
                <StreamingMd compact={compact} t={t} text={boundedLiveRenderText(text ?? '')} />
              )
            )}
          </Box>
        ) : null}
      </Box>
    )
  }

  const renderWork = (seg: Extract<Segment, { kind: 'work' }>) => {
    // The one tool whose result is a picture: a summary line cannot say which
    // node failed, so a stretch holding one opens itself, and folding it by hand
    // still leaves the graph.
    const hasDag = seg.tools.some(tool => tool.dag)
    // A solo stretch has no "expanded" depth of its own: its fold state gates the
    // detail block directly (WorkSegment's solo branch above), so defaulting that
    // state open would auto-expand the detail block, not just draw the graph
    // (which renders unconditionally there regardless). Only a multi-call
    // stretch's fold state means "show one row per call," which a dag call
    // should still default open.
    const detailDefaultOpen = hasDag && seg.tools.length > 1

    return (
      <WorkSegment
        compact={compact}
        defaultOpen={hasDag}
        isOpen={foldOpen(`seg:${seg.key}`, detailDefaultOpen)}
        key={`w:${seg.key}`}
        live={seg.live}
        now={now}
        openCalls={openCalls}
        t={t}
        toggleCall={id => toggle(`call:${id}`)}
        toggleSelf={() => toggleFold(scope, `seg:${seg.key}`, detailDefaultOpen)}
        tools={seg.tools}
        width={width}
      />
    )
  }

  const segments = segmentTurn(episodes, liveIndex)

  // The running episode only gets a talk segment once it has said something --
  // and the closing answer streams through `text` while `narration` is still
  // empty, because nothing flushes it into narration until the message ends. So
  // an episode that only answers has nowhere to put its stream, and the tail
  // block below is the one thing that can show it.
  const liveTalk = segments.some(seg => seg.kind === 'talk' && seg.episode.index === liveIndex)

  return (
    <Box flexDirection="column" width={width}>
      {segments.map((seg, i) => (
        <Box
          flexDirection="column"
          key={seg.kind === 'talk' ? `t:${seg.episode.index}` : `w:${seg.key}`}
          marginTop={i > 0 ? 1 : 0}
        >
          {seg.kind === 'talk' ? renderTalk(seg.episode) : renderWork(seg)}
        </Box>
      ))}

      {text && (!live || !liveTalk) ? (
        <Box marginTop={segments.length > 0 ? 1 : 0}>
          {prose(
            live ? (
              <StreamingMd compact={compact} t={t} text={boundedLiveRenderText(text)} />
            ) : (
              <Md avail={proseWidth} compact={compact} t={t} text={text} />
            )
          )}
        </Box>
      ) : null}
    </Box>
  )
})

// History path: a committed `kind: 'episodes'` message.
// A per-message identity for the fold scope, stable while the message grows.
//
// The fold ids are not all unique on their own: `seg:` and `call:` carry the
// transport's call ids, but `rsn:<n>` is an index that restarts at 0 in every
// message. One scope per *view* therefore put every turn's first thought under
// one key, and opening one turn's reasoning opened all of them.
//
// Two stable discriminators, in this order:
//
//  - the message's first tool call, which is the transport's own id and is the
//    same string before and after the turn lands, so a fold survives the settle
//    read as well as the polls;
//  - the row the message was folded from (`foldId`), for a turn that called
//    nothing at all -- it only thought and answered. That message's text grows
//    while it streams, so it is replaced on every poll, and keying it on the
//    object closed the reader's fold each time.
//
// The object is the last resort, for a message that came from neither -- the
// main transcript's own rows, which are not replaced while they are read.
const foldIds = new WeakMap<object, string>()
let foldSeq = 0

const messageFoldId = (msg: Msg): string => {
  const firstCall = msg.episodes?.find(ep => ep.tools.length > 0)?.tools[0]?.id

  if (firstCall !== undefined) {
    return firstCall
  }

  if (msg.foldId !== undefined) {
    return msg.foldId
  }

  const hit = foldIds.get(msg)

  if (hit !== undefined) {
    return hit
  }

  const next = `m${++foldSeq}`

  foldIds.set(msg, next)

  return next
}

export const EpisodeMessage = memo(function EpisodeMessage({
  cols,
  compact,
  msg,
  t
}: {
  cols?: number
  compact?: boolean
  msg: Msg
  t: Theme
}) {
  // The view is read here rather than threaded down as a prop: only one
  // transcript is ever on screen, and this is the adapter that knows which. The
  // message part keeps two turns in that view from sharing a fold.
  const scope = `${viewKeyOf(useStore($directChat).active)}:${messageFoldId(msg)}`

  return <EpisodeView cols={cols} compact={compact} episodes={msg.episodes ?? []} scope={scope} t={t} text={msg.text} />
})
