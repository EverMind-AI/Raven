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
  cardDefaultOpen,
  detailBlockShape,
  failureNote,
  segmentTurn,
  toolArgument,
  toolParts,
  toolsSummary,
  totalDurationMs
} from '../domain/episodeSummary.js'
import { fmtDuration } from '../domain/messages.js'
import { spawnRunSettled } from '../domain/spawnRun.js'
import { TRANSCRIPT_GUTTER_INSET, transcriptGutterWidth } from '../lib/inputMetrics.js'
import { hasMeaningfulReasoning } from '../lib/reasoning.js'
import { boundedLiveRenderText, compactPreview, tailPreview, clipToWidthFromEnd } from '../lib/text.js'
import { DagPanel } from './dagPanel.js'
import { Md } from './markdown.js'
import { SpawnPanel } from './spawnPanel.js'
import { StreamingMd } from './streamingMarkdown.js'
import { Spinner } from './thinking.js'

// Everything the transcript renders sits one step in from the edge, which is
// also where a normal assistant message's body starts (the width messageLine
// reserves for its gutter). Prose spends that step on the same reply marker;
// activity leaves it blank. Either way the body column is shared on purpose:
// inventing a second left edge would misalign this message kind from every
// other one. Depth steps in from there.
// The activity column starts where the reply glyph's does -- the marker margin
// is shared with it (see ActivityRow), so it is derived from the same helper
// rather than restated as a literal that has to be kept equal by hand.
const INDENT = transcriptGutterWidth('assistant', '')
const STEP = 2
const MIN_TAIL = 12

// The part of a click a fold control needs. Declared structurally rather than
// imported: the fork does not export its ClickEvent, and appLayout.tsx already
// names its own slice of the same event this way.
type FoldClick = { stopImmediatePropagation?: () => void }

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

// The expanded payload of one call: its full argument, then its output. No tree
// rails inside -- the block's own ground already scopes it, and the two are told
// apart by weight (argument in text color, output dim).
const DetailBlock = memo(function DetailBlock({
  argument,
  compact,
  indent,
  more,
  onToggle,
  output,
  t,
  width
}: {
  argument: string
  compact?: boolean
  /** How far in the payload's text sits. Spent as left padding rather than as
   *  a margin so the block's ground runs to the same left edge the call's own
   *  row does: the two are one card, and a slab starting four columns in under
   *  a row starting at zero reads as two. */
  indent: number
  /** The row that moves the card between its capped and full levels, on a row of
   *  its own. It rode inside the output array once, which gave it the block's own
   *  click -- so the one row announcing there was more to see was the row that
   *  collapsed the card. Revealed, it turns around and folds back to the cap;
   *  the block's own click is still the way out to a single row. */
  more?: { label: string; onToggle: () => void }
  onToggle?: () => void
  output: string[]
  t: Theme
  width: number
}) {
  const fill = canFill()
  const body = Math.max(8, width - indent - 2)

  return (
    <Box
      flexDirection="column"
      marginBottom={1}
      onClick={onToggle}
      paddingBottom={1}
      paddingLeft={indent + 1}
      paddingRight={1}
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

      {more ? (
        <Box
          onClick={(e: FoldClick) => {
            // A click bubbles up through every ancestor handler (see
            // dispatchClick), and this row sits inside the block whose own
            // handler collapses the card. Without this, moving a level and
            // folding the card away both fire on the same click.
            e.stopImmediatePropagation?.()
            more.onToggle()
          }}
        >
          {fill ? null : (
            <NoSelect fromLeftEdge>
              <Text color={t.color.border}>{'▏'}</Text>
            </NoSelect>
          )}
          <Box width={body}>
            <Text color={t.color.accent} wrap="truncate-end">
              {more.label}
            </Text>
          </Box>
        </Box>
      ) : null}
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
// The person's words merged into a running turn, drawn where they landed: one
// indented line, not the filled card a prompt gets, because a steer did not
// open a turn -- it bent the one already running.
const SteerRow = memo(function SteerRow({
  at,
  t,
  text,
  width
}: {
  at?: number
  t: Theme
  text: string
  width: number
}) {
  const when = at ? new Date(at).toTimeString().slice(0, 5) : ''

  return (
    <Box width={width}>
      <Box flexShrink={0} width={INDENT} />
      <Text>
        <Text color={t.color.accent}>{'\u21b3 steer'}</Text>
        {when ? <Text color={t.color.muted}>{` ${when}`}</Text> : null}
        <Text color={t.color.muted}>{' \u00b7 '}</Text>
        <Text bold>{text}</Text>
      </Text>
    </Box>
  )
})

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
  // Two states, two words. While the model is still at it the row is a
  // present-tense label and a clock that moves; once it has stopped, leaving it
  // reading "reasoning" says the model is still thinking about a step it
  // finished several tool calls ago. Past tense, and the span it took.
  const head = running ? `reasoning${time ? ` (${time})` : ''}` : time ? `thought for ${time}` : 'thought'
  // ` \u00b7 ` costs 3, and a tail shorter than this is more ellipsis than text.
  const room = width - INDENT - stringWidth(head) - 3
  const trail = tail && room >= MIN_TAIL ? clipToWidthFromEnd(tail, room) : ''

  return (
    <Box width={width}>
      <Box flexShrink={0} paddingLeft={TRANSCRIPT_GUTTER_INSET} width={INDENT}>
        {running ? (
          <NoSelect fromLeftEdge>
            <Text>
              <Spinner color={t.color.accent} variant="tool" />
            </Text>
          </NoSelect>
        ) : (
          <NoSelect fromLeftEdge>
            <Text color={t.color.label}>{'\u00b7'}</Text>
          </NoSelect>
        )}
      </Box>

      <Box flexGrow={1} minWidth={0} onClick={onToggle}>
        <Text wrap="truncate-end">
          <Text color={t.color.muted}>{head}</Text>
          {trail ? <Text color={t.color.label}>{` \u00b7 ${trail}`}</Text> : null}
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
//
// The margin is on top, not bottom: every follower (the reply, the next
// segment) already opens with one, so a bottom margin here put the answer two
// blank rows below the scratch work while the label sat flush against it.
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
      marginTop={1}
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

// One activity row: a verb, its target, an inline duration, and an outcome in
// the margin. Three inks, because a row is three things and painting them one
// value let the target win on length alone -- a path is the longest part and the
// last one a reader needs. There is no fold marker -- a summary row announces
// that it summarizes, and repeating that as a glyph on every row is what made
// the transcript look like a control panel. Expandability is a property of the
// whole activity column, not of each row.
//
// Nothing here is `dim`: `muted` is already the de-emphasis, and stacking SGR 2
// on top of it put every row in this view under 3:1 -- by an amount the terminal
// decides for itself. Same call the aside block above already made, for the same
// reason.
//
// The row stands on the same filled ground its detail block uses, so a call and
// what it returned read as one object rather than as two runs of transcript --
// the ground the DAG and spawn panels get from a border, at the same width and
// without spending two rows on one. Nothing inside the fill moves: the label
// column is shared with the reasoning row and the reply, and a card that indents
// its own text would be the only row in the view aligned with nothing.
const ActivityRow = memo(function ActivityRow({
  depth,
  done,
  failed,
  label,
  note,
  onToggle,
  opened,
  running,
  t,
  time,
  verb,
  width
}: {
  depth: number
  /** A settled call or stretch, so the margin can say how it went. Work still
   *  queued, and the in-flight summary whose spinner already owns that cell,
   *  leave it unset and keep the margin empty. */
  done?: boolean
  failed?: boolean
  label: string
  note?: string
  /** Heading an expanded detail block, so the card wants a blank row above its
   *  first line the way the user's slab does. Never set on a collapsed row: the
   *  height estimator counts a folded stretch as exactly one row, and a line
   *  item that spends three rows on one line of text is not a card. */
  opened?: boolean
  onToggle?: () => void
  running?: boolean
  t: Theme
  time?: string
  /** The head of a single call's row -- `read`, `ran`, `listed`. Carries the
   *  row's emphasis so the target beside it can sit a step back, which is what
   *  lets a reader scan a column of verbs instead of a column of paths. A folded
   *  summary row is a sentence rather than a verb plus a target, and leaves it
   *  unset. */
  verb?: string
  width: number
}) {
  // Outcome as a glyph, not as the row's colour. Recolouring the whole row red
  // made the row a reader most needs to read the hardest one to read, and left a
  // finished call indistinguishable from one that never started. A glyph also
  // survives a terminal with no colour, where a red row is just a row.
  const marker = failed ? '\u2717' : done ? '\u2713' : ''

  return (
    <Box paddingTop={opened ? 1 : 0} width={width} {...(canFill() && { backgroundColor: t.color.detailBg })}>
      {/* The spinner goes in the marker margin the reasoning row and the reply
          glyph already share, not inline ahead of the label. Inline, a row's
          text sat two columns right of every other row for as long as the call
          ran and snapped back when it landed -- the running row, the one a
          reader is actually watching, was the only row aligned with nothing. */}
      <Box flexShrink={0} paddingLeft={TRANSCRIPT_GUTTER_INSET} width={INDENT}>
        {running ? (
          <NoSelect fromLeftEdge>
            <Text>
              <Spinner color={t.color.accent} variant="tool" />
            </Text>
          </NoSelect>
        ) : marker ? (
          <NoSelect fromLeftEdge>
            <Text color={failed ? t.color.error : t.color.ok}>{marker}</Text>
          </NoSelect>
        ) : null}
      </Box>
      <Box flexGrow={1} minWidth={0} onClick={onToggle} paddingLeft={Math.max(0, depth - INDENT)} paddingRight={1}>
        <Text wrap="truncate-end">
          {verb ? <Text color={t.color.text}>{`${verb} `}</Text> : null}
          <Text color={t.color.muted}>{label}</Text>
          {/* A failure note names which call broke, which is the whole reason a
              folded stretch does not auto-expand -- so it keeps the colour the
              row itself gave up. */}
          {note ? <Text color={failed ? t.color.error : t.color.muted}>{` · ${note}`}</Text> : null}
          {time ? <Text color={t.color.label}>{` (${time})`}</Text> : null}
        </Text>
      </Box>
    </Box>
  )
})

// What a card's fold would be if the reader had never touched it. `toggleFold`
// flips against this, so passing a flat `false` here would make the first click
// on an already-open card a no-op: it would "open" what is open.
const cardToggleDefault = (tools: EpisodeTool[], id: string): boolean => {
  const tool = tools.find(t => t.id === id)

  return tool !== undefined && Boolean(tool.done) && cardDefaultOpen(tool)
}

// A stretch of work between two things the model said. Three depths:
//   folded  -- one row: "listed .raven/, read TOOLS.md, ran 4 commands (2.4s)"
//   open    -- one row per call
//   detail  -- a call's full argument and output, in a filled block
// A single-call stretch skips the middle depth: its folded row already names the
// call, so an identical row underneath would just be the same sentence twice.
// A dag call is the one exception to "folded is one row" -- see dagFor below.
const WorkSegment = memo(function WorkSegment({
  closedCalls,
  compact,
  defaultOpen,
  isOpen,
  live,
  now,
  openCalls,
  openFull,
  t,
  toggleCall,
  toggleFull,
  toggleSelf,
  tools,
  width
}: {
  /** Cards the reader shut. Held apart from `openCalls` for the same reason
   *  `foldStore` holds both sets: without it, "shut" and "never touched" are one
   *  value, and a card that defaults open reopens itself the moment its row is
   *  rebuilt mid-turn. */
  closedCalls: ReadonlySet<string>
  compact?: boolean
  defaultOpen: boolean
  isOpen: boolean
  live: boolean
  now: number
  openCalls: ReadonlySet<string>
  openFull: ReadonlySet<string>
  t: Theme
  toggleCall: (id: string) => void
  toggleFull: (id: string) => void
  toggleSelf: () => void
  tools: EpisodeTool[]
  width: number
}) {
  const inFlight = live && tools.some(tool => !tool.done)
  const failure = failureNote(tools)
  const summaryRoom = Math.max(8, width - INDENT)
  const solo = tools.length === 1

  // One call's card, resolved the way `foldStore` resolves any fold: an explicit
  // decision either way wins, and only an untouched card falls through to the
  // predicate.
  //
  // Gated on the call having landed, not on the turn having ended. A call that
  // is done is not going to change again, so opening it costs no churn -- and
  // reading the stretch's own live flag here made a finished call sit under its
  // check mark for the rest of the turn, opening a second or two later when the
  // row committed. What must not open is a call still running.
  const cardOpen = (tool: EpisodeTool) =>
    openCalls.has(tool.id) ? true : closedCalls.has(tool.id) ? false : Boolean(tool.done) && cardDefaultOpen(tool)

  // A dag call has no row of its own: its panel is a titled box carrying the
  // call, the graph and the tally, and the row above it said the first of those
  // a second time -- with the node ids spelled out again, which is what the box
  // header replaced. A spawn call renders as a panel on the same terms.
  // `opened` is whether a detail block actually follows, not whether the fold is
  // open: a call with no argument and no output renders no block, and a blank row
  // above nothing is a one-line band spending two rows.
  const callRow = (tool: EpisodeTool, depth: number, onToggle: () => void, opened = false) => {
    if (tool.dag || tool.spawn) {
      return null
    }

    const parts = toolParts(tool)
    const running = live && !tool.done
    const failed = callFailed(tool)

    return (
      <ActivityRow
        depth={depth}
        done={tool.done}
        failed={failed}
        key={tool.id}
        label={parts.detail}
        onToggle={onToggle}
        opened={opened}
        running={running}
        t={t}
        time={durationLabel(elapsedOf(tool, now), running)}
        verb={parts.verb}
        width={width}
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
        <DagPanel now={now} run={tool.dag} t={t} width={Math.max(28, width - depth)} />
      </Box>
    ) : null

  // A spawn call renders on the dag call's terms: a titled panel that carries
  // the call, at every depth -- including the folded summary row. Its trace box
  // opens itself while the run works (see `spawnOpen`), which is what a single
  // delegated run has instead of a graph to watch.
  const spawnFor = (tool: EpisodeTool, depth: number) =>
    tool.spawn ? (
      <Box key={`s:${tool.id}`} paddingLeft={depth}>
        <SpawnPanel now={now} prompt={toolArgument(tool)} run={tool.spawn} t={t} width={Math.max(28, width - depth)} />
      </Box>
    ) : null

  // Three levels, not two: the card shows a capped preview, the `full` fold shows
  // what the cap left behind, and either one folds back to the row. The shape
  // comes from the domain layer so the height estimator measures the same block
  // this draws -- deciding it in both places is how the two drift apart.
  const detailFor = (tool: EpisodeTool, depth: number, onCollapse: () => void) => {
    const full = openFull.has(tool.id)
    const shape = detailBlockShape(tool, full)

    if (!shape) {
      return null
    }

    return (
      <Box key={`d:${tool.id}`}>
        <DetailBlock
          argument={shape.argument}
          compact={compact}
          indent={depth}
          more={
            shape.more
              ? {
                  label:
                    shape.more.kind === 'reveal'
                      ? `… +${shape.more.count} lines`
                      : // A result long enough to outrun TOOL_FULL_ROWS is clipped
                        // even here, and this row is the only place that can say so.
                        `… -${shape.more.count} lines${shape.hidden > 0 ? ` (+${shape.hidden} still hidden)` : ''}`,
                  onToggle: () => toggleFull(tool.id)
                }
              : undefined
          }
          onToggle={onCollapse}
          output={shape.output}
          t={t}
          width={width}
        />
      </Box>
    )
  }

  // ── Running: the summary so far, plus only the call in hand ──
  //
  // Quiet by default and openable all the same. What a reader wants from a call
  // is most urgent while it runs -- the command a two-minute `exec` is actually
  // running is a question that cannot wait for it to finish -- so the rows here
  // carry the same toggles the settled ones do. Only the default differs: the
  // stretch stays compressed to the summary and the call in hand until the
  // reader opens it (see `detailDefaultOpen`, which does not fire in flight).
  if (inFlight) {
    const latest = [...tools].reverse().find(tool => !tool.done) ?? tools[tools.length - 1]!
    const done = totalDurationMs(tools.filter(tool => tool.done))
    const parts = toolParts(latest)
    const elapsed = elapsedOf(latest, now)

    // One call in the whole stretch means the summary IS the call: printing
    // both gives the same sentence twice, under two spinners. So it opens the
    // way a settled lone call does -- its row straight into its detail.
    if (solo) {
      const detail = isOpen ? detailFor(latest, INDENT, toggleSelf) : null

      return (
        <Box flexDirection="column">
          {callRow(latest, INDENT, toggleSelf, Boolean(detail))}
          {dagFor(latest, INDENT)}
          {spawnFor(latest, INDENT)}
          {detail}
        </Box>
      )
    }

    return (
      <Box flexDirection="column">
        <ActivityRow
          depth={INDENT}
          label={toolsSummary(tools, summaryRoom)}
          onToggle={toggleSelf}
          // Opened, the running call's own row carries the spinner, and two of
          // them twitching at once is what this row's was competing with.
          running={!isOpen}
          t={t}
          time={durationLabel(done, false)}
          width={width}
        />
        {isOpen ? (
          tools.map(tool => {
            const detail = cardOpen(tool) ? detailFor(tool, INDENT + STEP, () => toggleCall(tool.id)) : null

            return (
              <Box flexDirection="column" key={tool.id}>
                {callRow(tool, INDENT + STEP, () => toggleCall(tool.id), Boolean(detail))}
                {dagFor(tool, INDENT + STEP)}
                {spawnFor(tool, INDENT + STEP)}
                {detail}
              </Box>
            )
          })
        ) : (
          <>
            {/* The spinner above already says "in flight"; a second one on the
                call in hand just makes two things twitch at once. */}
            {latest.dag || latest.spawn ? null : (
              <ActivityRow
                depth={INDENT + STEP}
                label={parts.detail}
                onToggle={() => toggleCall(latest.id)}
                t={t}
                time={durationLabel(elapsed, true)}
                verb={parts.verb}
                width={width}
              />
            )}
            {dagFor(latest, INDENT + STEP)}
            {spawnFor(latest, INDENT + STEP)}
            {cardOpen(latest) ? detailFor(latest, INDENT + STEP, () => toggleCall(latest.id)) : null}
          </>
        )}
      </Box>
    )
  }

  const total = totalDurationMs(tools)

  // ── One call: the folded row IS the call; opening goes straight to detail ──
  if (solo) {
    const tool = tools[0]!
    const detail = isOpen ? detailFor(tool, INDENT, toggleSelf) : null

    return (
      <Box flexDirection="column">
        {callRow(tool, INDENT, toggleSelf, Boolean(detail))}
        {dagFor(tool, INDENT)}
        {spawnFor(tool, INDENT)}
        {detail}
      </Box>
    )
  }

  return (
    <Box flexDirection="column">
      <ActivityRow
        depth={INDENT}
        done
        failed={Boolean(failure)}
        label={toolsSummary(tools, summaryRoom)}
        note={failure || undefined}
        onToggle={toggleSelf}
        t={t}
        time={durationLabel(total, false)}
        width={width}
      />
      {isOpen
        ? tools.map(tool => {
            const detail = cardOpen(tool) ? detailFor(tool, INDENT + STEP, () => toggleCall(tool.id)) : null

            return (
              <Box flexDirection="column" key={tool.id}>
                {callRow(tool, INDENT + STEP, () => toggleCall(tool.id), Boolean(detail))}
                {dagFor(tool, INDENT + STEP)}
                {spawnFor(tool, INDENT + STEP)}
                {detail}
              </Box>
            )
          })
        : defaultOpen
          ? tools.map(tool => [dagFor(tool, INDENT + STEP), spawnFor(tool, INDENT + STEP)])
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
  dense = false,
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
  /** Trace-box rendering: no breathing rows between segments, and a static
   *  reasoning row carries a tail of its text instead of standing bare. The
   *  height estimator's own `dense` must agree (see `estimatedMsgHeight`). */
  dense?: boolean
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
  // A delegated run outlives its turn: the panel pinned onto its tool row only
  // re-renders on status frames, which for a long run means minutes between
  // repaints -- and an elapsed column frozen at whatever the clock said when
  // the reply committed. So the ticker also runs while any pinned run is still
  // working, not only while the turn itself is live.
  const working = episodes.some(ep =>
    ep.tools.some(tool => (tool.dag && !tool.dag.done) || (tool.spawn && !spawnRunSettled(tool.spawn)))
  )
  const now = useNow((live || working) && episodes.length > 0)
  const width = cols ? Math.max(20, cols - 4) : 116
  const proseWidth = Math.max(20, width - INDENT)
  const liveIndex = live ? episodes[lastIdx]?.index : undefined
  const idsFor = (keys: Iterable<string>, prefix: string) =>
    new Set([...keys].filter(k => k.startsWith(prefix)).map(k => k.slice(prefix.length)))
  const openCalls = idsFor(seeded.open, 'call:')
  // `seeded.open` already merges the store's own open ids; `closed` is read from
  // both sources at the point of use (see foldOpen), so the card sets merge it
  // here instead.
  const closedCalls = idsFor([...seeded.closed, ...(stored[scope]?.closed ?? [])], 'call:')
  const openFull = idsFor(seeded.open, 'full:')

  // The model's prose carries the same reply marker `messageLine` draws in its
  // gutter (`ROLE.assistant`). Drawn here because this view owns the live turn:
  // without it a reply stayed unmarked until the turn settled into a plain
  // history row, so the marker looked like something the turn earned by
  // finishing. The glyph sits inside the INDENT the prose already had, so the
  // body column does not move and activity rows still line up with it.
  const prose = (node: ReactNode) => (
    <Box>
      <Box flexShrink={0} paddingLeft={TRANSCRIPT_GUTTER_INSET} width={INDENT}>
        <Text color={t.color.muted}>{t.brand.tool}</Text>
      </Box>

      <Box flexDirection="column" width={proseWidth}>
        {node}
      </Box>
    </Box>
  )

  const renderTalk = (ep: Episode) => {
    if (ep.steer !== undefined) {
      return <SteerRow at={ep.steerAtMs} key={`t:${ep.index}`} t={t} text={ep.steer} width={width} />
    }

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
              // In a trace box the row is static, so without the tail it reads
              // as a bare label -- the one word "reasoning" says nothing.
              tail={(thinking || dense) && !rsnOpen ? reasoning : ''}
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
          <Box marginTop={hasReasoning && !dense ? 1 : 0}>
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
    // The tools whose result is a panel: a summary line cannot say which node
    // failed or what a delegated run is doing, so a stretch holding one opens
    // itself, and folding it by hand still leaves the panel.
    const hasPanel = seg.tools.some(tool => tool.dag || tool.spawn)
    // A solo stretch has no "expanded" depth of its own: its fold state gates the
    // detail block directly (WorkSegment's solo branch above), so for one call
    // this state IS the card's own fold, and the card's predicate is what answers
    // it. A multi-call stretch's fold state means "show one row per call" -- a
    // different question, left as it was: the summary row is the compression, and
    // each card inside it resolves its own default once the stretch is open.
    //
    // A solo panel call keeps the old answer. Its graph renders unconditionally
    // at every depth, so opening the fold would add a detail block under a panel
    // that already is the result, not reveal one.
    //
    // For one call this waits on the call, not on the turn: `WorkSegment` already
    // leaves the in-flight branch the moment the call is done, and holding the
    // block back until the row committed is what put a second or two between the
    // check mark and the card. A multi-call stretch still waits out the turn --
    // there this state decides whether every call gets a row, and a stretch that
    // unfolds itself as the calls land is the churn the compressed live view
    // exists to avoid. A reader's own open still stands -- `foldOpen` reads it
    // first.
    const detailDefaultOpen =
      seg.tools.length === 1
        ? !hasPanel && Boolean(seg.tools[0]!.done) && cardDefaultOpen(seg.tools[0]!)
        : hasPanel && !seg.live

    return (
      <WorkSegment
        closedCalls={closedCalls}
        compact={compact}
        defaultOpen={hasPanel}
        isOpen={foldOpen(`seg:${seg.key}`, detailDefaultOpen)}
        key={`w:${seg.key}`}
        live={seg.live}
        now={now}
        openCalls={openCalls}
        openFull={openFull}
        t={t}
        toggleCall={id => toggleFold(scope, `call:${id}`, cardToggleDefault(seg.tools, id))}
        toggleFull={id => toggle(`full:${id}`)}
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
          marginTop={i > 0 && !dense ? 1 : 0}
        >
          {seg.kind === 'talk' ? renderTalk(seg.episode) : renderWork(seg)}
        </Box>
      ))}

      {text && (!live || !liveTalk) ? (
        <Box marginTop={segments.length > 0 && !dense ? 1 : 0}>
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
// Three stable discriminators, in this order:
//
//  - the turn the message came from (`foldId`): minted at message.start and
//    published on the turn state, so the live view names this scope before the
//    turn has called anything, and the committed message names the same one.
//    A resumed transcript sets it from the fold's own first call id
//    (`episodeFold`), which is the string the live read used, so the two agree
//    across a restart as well;
//  - the message's first tool call, for a message that carries no `foldId` --
//    the transport's own id, and the same string before and after the settle;
//  - the object, as a last resort, for a message that came from neither. Only
//    safe for rows that are not replaced while they are read: a message rebuilt
//    on every poll gets a new id each time, which closes the reader's fold.
const foldIds = new WeakMap<object, string>()
let foldSeq = 0

/** One transcript view's fold namespace for one turn. A live read and the
 * settled row MUST resolve to the same string, or every fold the reader opened
 * mid-turn is lost the moment the turn lands. */
export const turnFoldScope = (viewKey: string, turnId: string): string => `${viewKey}:${turnId}`

export const messageFoldId = (msg: Msg): string => {
  if (msg.foldId !== undefined) {
    return msg.foldId
  }

  const firstCall = msg.episodes?.find(ep => ep.tools.length > 0)?.tools[0]?.id

  if (firstCall !== undefined) {
    return firstCall
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
  dense,
  msg,
  t
}: {
  cols?: number
  compact?: boolean
  dense?: boolean
  msg: Msg
  t: Theme
}) {
  // The view is read here rather than threaded down as a prop: only one
  // transcript is ever on screen, and this is the adapter that knows which. The
  // message part keeps two turns in that view from sharing a fold.
  const scope = turnFoldScope(viewKeyOf(useStore($directChat).active), messageFoldId(msg))

  return (
    <EpisodeView
      cols={cols}
      compact={compact}
      dense={dense}
      episodes={msg.episodes ?? []}
      scope={scope}
      t={t}
      text={msg.text}
    />
  )
})
