// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { activeColorTier, Box, NoSelect, Text } from '@hermes/ink'
import { memo, useEffect, useState } from 'react'

import type { Segment } from '../domain/episodeSummary.js'
import type { Theme } from '../theme.js'
import type { Episode, EpisodeTool, Msg } from '../types.js'

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
import { boundedLiveRenderText, compactPreview, tailPreview } from '../lib/text.js'
import { DagPanel } from './dagPanel.js'
import { Md } from './markdown.js'
import { StreamingMd } from './streamingMarkdown.js'
import { Spinner } from './thinking.js'

// Everything the transcript renders sits one step in from the edge, which is
// also where a normal assistant message's body starts (messageLine reserves a
// `┊ ` gutter that this view does not draw). Activity shares that margin with
// prose on purpose: inventing a second left edge would misalign this message
// kind from every other one. Depth steps in from there.
const INDENT = 2
const STEP = 2

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
const WorkSegment = memo(function WorkSegment({
  compact,
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
  // call's own row rather than inside the detail block -- at every depth where
  // that row is the call itself. Under a summary row it does not: a folded
  // stretch is one row, and a graph is not one row.
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
        : null}
    </Box>
  )
})

// Renders a turn as an alternating stream of what the model said and what the
// machine did. Prose keeps the transcript's normal margin and color; activity
// shares the margin but is dim and carries an inline duration -- that pairing,
// not a column of glyphs, is what separates the two voices.
export const EpisodeView = memo(function EpisodeView({
  cols,
  compact,
  episodes,
  live = false,
  openKeys,
  t,
  text
}: {
  cols?: number
  compact?: boolean
  episodes: Episode[]
  live?: boolean
  openKeys?: readonly string[]
  t: Theme
  text?: string
}) {
  // One fold set: `seg:<id>` a stretch of work, `call:<id>` one call's detail,
  // `rsn:<n>` an episode's chain of thought. `openKeys` seeds it -- the folds are
  // click-driven, so this is the only way to render an opened one.
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set(openKeys))
  const toggle = (key: string) =>
    setOpen(prev => {
      const next = new Set(prev)

      if (!next.delete(key)) {
        next.add(key)
      }

      return next
    })

  const lastIdx = episodes.length - 1
  const now = useNow(live && episodes.length > 0)
  const width = cols ? Math.max(20, cols - 4) : 116
  const proseWidth = Math.max(20, width - INDENT)
  const liveIndex = live ? episodes[lastIdx]?.index : undefined
  const openCalls = new Set([...open].filter(k => k.startsWith('call:')).map(k => k.slice(5)))

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
    const rsnOpen = (thinking && hasReasoning) || open.has(`rsn:${ep.index}`)

    return (
      <Box flexDirection="column" key={`t:${ep.index}`}>
        {hasReasoning ? (
          <>
            <ActivityRow
              depth={INDENT}
              label="reasoning"
              onToggle={() => toggle(`rsn:${ep.index}`)}
              running={thinking}
              t={t}
              time={durationLabel(reasoningMs, Boolean(thinking))}
              width={Math.max(8, width - INDENT)}
            />
            {rsnOpen ? (
              <Box paddingLeft={INDENT + STEP}>
                <DetailBlock
                  argument=""
                  compact={compact}
                  onToggle={() => toggle(`rsn:${ep.index}`)}
                  output={[(thinking ? tailPreview : compactPreview)(reasoning, 4000)]}
                  t={t}
                  width={Math.max(12, width - INDENT - STEP)}
                />
              </Box>
            ) : null}
          </>
        ) : null}

        {narration || (live && running && text) ? (
          <Box marginTop={hasReasoning ? 1 : 0} paddingLeft={INDENT}>
            {narration ? (
              <Md avail={proseWidth} compact={compact} t={t} text={narration} />
            ) : (
              <StreamingMd compact={compact} t={t} text={boundedLiveRenderText(text ?? '')} />
            )}
          </Box>
        ) : null}
      </Box>
    )
  }

  const renderWork = (seg: Extract<Segment, { kind: 'work' }>) => (
    <WorkSegment
      compact={compact}
      isOpen={open.has(`seg:${seg.key}`)}
      key={`w:${seg.key}`}
      live={seg.live}
      now={now}
      openCalls={openCalls}
      t={t}
      toggleCall={id => toggle(`call:${id}`)}
      toggleSelf={() => toggle(`seg:${seg.key}`)}
      tools={seg.tools}
      width={width}
    />
  )

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
        <Box marginTop={segments.length > 0 ? 1 : 0} paddingLeft={INDENT}>
          {live ? (
            <StreamingMd compact={compact} t={t} text={boundedLiveRenderText(text)} />
          ) : (
            <Md avail={proseWidth} compact={compact} t={t} text={text} />
          )}
        </Box>
      ) : null}
    </Box>
  )
})

// History path: a committed `kind: 'episodes'` message.
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
  return <EpisodeView cols={cols} compact={compact} episodes={msg.episodes ?? []} t={t} text={msg.text} />
})
