// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import type { Episode, EpisodeTool } from '../types.js'

import { hasMeaningfulReasoning } from '../lib/reasoning.js'
import { claudeRule } from './claudeCodeTools.js'
import { codexRule } from './codexTools.js'

// Per-tool phrasing. `style` decides how a run of the same tool collapses:
//   count  — homogeneous info tools -> "read 6 files"; single -> the target
//   target — heterogeneous/mutating tools -> show the target; many -> "ran 3 commands"
// A rule's verb is whichever vocabulary its own table uses: Raven's own plain
// lowercase in OVERRIDES below, or an adapter's label verbatim in CODEX_VERBS /
// CLAUDE_VERBS.
export interface VerbRule {
  verb: string
  unit: string
  style: 'count' | 'target'
}

// OVERRIDES are polish, not the source of truth: a hand-picked verb only for the
// tools whose phrasing we're confident about. Every other tool — one we haven't
// listed, or one added later — is handled by `ruleFor` below, which derives a
// readable label from the tool name itself. So a new backend tool renders
// sensibly with zero changes here, instead of silently degrading to "ran".
// Tool names are the real backend names (raven/agent/tools/*.py).
const OVERRIDES: Record<string, VerbRule> = {
  read_file: { verb: 'read', unit: 'files', style: 'count' },
  grep: { verb: 'searched', unit: 'patterns', style: 'count' },
  find: { verb: 'found', unit: 'files', style: 'count' },
  list_dir: { verb: 'listed', unit: 'dirs', style: 'count' },
  web_search: { verb: 'searched', unit: 'queries', style: 'target' },
  web_fetch: { verb: 'fetched', unit: 'urls', style: 'target' },
  exec: { verb: 'ran', unit: 'commands', style: 'target' },
  edit_file: { verb: 'edited', unit: 'files', style: 'target' },
  write_file: { verb: 'wrote', unit: 'files', style: 'target' },
  deep_research: { verb: 'researched', unit: '', style: 'target' },
  cron: { verb: 'scheduled', unit: '', style: 'target' },
  ask_user: { verb: 'asked', unit: '', style: 'target' },
  spawn: { verb: 'delegated', unit: 'subagents', style: 'count' }
}

// A tool name is a snake_case identifier; humanize it to a plain lowercase
// phrase used as the fallback verb: "web_search" -> "web search",
// "image_generate" -> "image generate". Never misleading, always maintenance-free.
const humanize = (name: string) => name.split('_').filter(Boolean).join(' ')

// The rule for any tool: its override if we have one, else an adapter's own
// table, else a generic rule built from the humanized name. The adapter tables
// are consulted after OVERRIDES rather than merged into it because the three are
// keyed by different vocabularies -- OVERRIDES by Raven's names, CODEX_VERBS by
// codex's, CLAUDE_VERBS by Claude Code's.
const ruleFor = (name: string): VerbRule =>
  OVERRIDES[name] ??
  codexRule(name) ??
  claudeRule(name) ?? { verb: humanize(name) || name, unit: 'calls', style: 'target' }

// Search-like tools read better with the needle quoted: searched "DeviceFlow".
const QUOTED = new Set(['grep', 'find', 'web_search', 'Grep', 'Glob', 'WebSearch'])

// A shell command is an argument, never a label: a 100-char pipeline as the row
// title is what made the transcript unreadable. Name the programs it runs
// instead -- `curl -s "…" | python3 -c "…"` becomes `curl -> python3` -- and
// leave the command itself for the expanded detail. Env assignments and `sudo`
// are stepped over so the reported program is the one doing the work.
//
// The model's own intent (`tool.intent`, read in `target()` below) wins when
// the transport sends one; this is the fallback for a call that carries none.
const SHELL_NOISE = new Set(['sudo', 'command', 'exec', 'time', 'nohup', 'env'])

// Split on shell operators that are NOT inside quotes. Naively splitting on `;`
// tore `python3 -c "import sys; ..."` in half and put the fragment on the row.
const shellStages = (command: string): string[] => {
  const stages: string[] = []
  let quote: '"' | "'" | null = null
  let current = ''

  for (let i = 0; i < command.length; i++) {
    const ch = command[i]!

    if (quote) {
      if (ch === quote && command[i - 1] !== '\\') {
        quote = null
      }

      current += ch
      continue
    }

    if (ch === '"' || ch === "'") {
      quote = ch
      current += ch
      continue
    }

    // A `&` touching a redirection is part of it (`2>&1`, `&>log`), not a stage
    // break: splitting there left `1` standing where a program name goes.
    const redirected = ch === '&' && (command[i - 1] === '>' || command[i + 1] === '>')

    if ((ch === ';' || ch === '|' || ch === '&') && !redirected) {
      // Collapse the two-character forms (`&&`, `||`) into one break.
      if (command[i + 1] === ch) {
        i++
      }

      stages.push(current)
      current = ''
      continue
    }

    current += ch
  }

  stages.push(current)

  return stages.filter(s => s.trim())
}

// `ruff check`, `git status`, `pip show` -- the subcommand is half the meaning,
// so a bare word right after the program joins it. A flag, a path, or anything
// quoted is an argument, and arguments belong in the expanded detail.
const isSubcommand = (word: string) => /^[a-z][\w-]*$/i.test(word)

// Plumbing a command is piped THROUGH, never the point of running it. Naming it
// buries the program that did the work ("ls -> head -> find").
const PLUMBING = new Set(['head', 'tail', 'wc', 'cat', 'less', 'more', 'tee', 'sort', 'uniq', 'xargs'])

// Where the work happened, not what it was.
const CHDIR = new Set(['cd', 'pushd', 'popd'])

// How many programs a row names before it stops being a label and starts being
// a command again.
const EXEC_LABEL_STAGES = 2

export const execLabel = (command: string): string => {
  const named: { label: string; program: string }[] = []
  const stages = shellStages(command)

  for (const stage of stages) {
    const words = stage.trim().split(/\s+/)
    // `FOO=bar cmd` -- step over assignments and wrappers to the real program.
    const at = words.findIndex(w => w && !w.includes('=') && !SHELL_NOISE.has(w))

    if (at < 0) {
      continue
    }

    // Checked before the basename strip, which would turn `2>/dev/null` into
    // `null` and name a device file as the program that did the work.
    if (/^\d*[<>]/.test(words[at]!)) {
      continue
    }

    const program = words[at]!.replace(/^.*\//, '')

    if (!program || program.startsWith('-')) {
      continue
    }

    // `cd /repo && git status` is one intent. Naming the chdir spends a slot on
    // where the work happened rather than on what it was -- but a lone `cd` is
    // still the whole command, so it only steps aside for another stage.
    if (CHDIR.has(program) && stages.length > 1) {
      continue
    }

    // `... | head -20` adds nothing; but a bare `head file` is the work itself.
    if (PLUMBING.has(program) && named.length) {
      continue
    }

    // `which raven || which hermes` is one intent, not two stages.
    if (named.at(-1)?.program === program) {
      continue
    }

    const next = words[at + 1]

    named.push({ label: next && isSubcommand(next) ? `${program} ${next}` : program, program })
  }

  if (!named.length) {
    // Nothing named a program -- a flag sat where one was expected, as in
    // `sudo -u postgres psql`. Name the wrapper rather than falling back to the
    // command itself: a row is a title, and the command is what the detail
    // block is for.
    return clip(command.trim().split(/\s+/)[0] ?? command, 40)
  }

  const shown = named.slice(0, EXEC_LABEL_STAGES).map(n => n.label)

  return named.length > EXEC_LABEL_STAGES ? `${shown.join(' -> ')} -> …` : shown.join(' -> ')
}

// Only path-like arguments are clipped from the LEFT (a path's meaning lives in
// its tail). Questions, commands and prose must keep their head, or the row
// becomes unreadable ("…体是指哪个？").
const PATHY = new Set([
  'read_file',
  'write_file',
  'edit_file',
  'list_dir',
  'apply_patch',
  'fileChange',
  'imageView',
  'commandExecution.read',
  'commandExecution.listFiles',
  'Read',
  'Write',
  'Edit',
  'NotebookEdit',
  'LS'
])

const titleName = (name: string) =>
  name
    .split('_')
    .filter(Boolean)
    .map(p => p[0]!.toUpperCase() + p.slice(1))
    .join(' ') || name

const clip = (s: string, n = 40) => {
  const one = s.replace(/\s+/g, ' ').trim()

  return one.length > n ? `${one.slice(0, n - 1)}…` : one
}

// Tools whose subject is a shell command, so the row names the programs it ran
// rather than printing the pipeline.
const SHELL = new Set(['exec', 'commandExecution', 'Bash', 'BashOutput'])

// The visible target for a single call: a file basename for path-like tools, a
// quoted needle for search tools, else the trimmed argument itself.
// A URL's identity is its host and path; the scheme is noise on a row that
// already says "fetched".
const shortUrl = (raw: string) => raw.replace(/^https?:\/\//, '').replace(/\/$/, '')

// Tools whose subject is a URL, so the row strips the scheme instead of keeping it.
const FETCHED_URL = new Set(['web_fetch', 'WebFetch'])

const target = (tool: EpisodeTool, budget?: number): string => {
  // The model's own description of the call, when the transport sent one. It
  // names the intent, which every derived label below can only approximate.
  if (tool.intent) {
    return clip(tool.intent, budget)
  }

  let raw = tool.summary.trim()

  // Some transports lead the summary with the tool's own name ("web_fetch:
  // https://..."); the verb already says that, so the row would say it twice
  // ("fetched web_fetch: ..."). Only a prefix with something after it is
  // stripped -- a bare echo of the name is all such a summary has to show.
  const prefix = `${tool.name}:`

  if (raw.toLowerCase().startsWith(prefix.toLowerCase()) && raw.length > prefix.length) {
    raw = raw.slice(prefix.length).trim() || raw
  }

  if (!raw) {
    return ''
  }

  if (PATHY.has(tool.name)) {
    return clip(raw.split(/[\\/]/).pop() || raw)
  }

  if (QUOTED.has(tool.name)) {
    // Room permitting the whole needle fits, which lets the detail block skip
    // repeating it as its argument line.
    return `"${clip(raw, Math.min(budget ?? 40, 72))}"`
  }

  if (SHELL.has(tool.name)) {
    return execLabel(raw)
  }

  if (FETCHED_URL.has(tool.name)) {
    return clip(shortUrl(raw), budget ?? 40)
  }

  return clip(raw, budget)
}

const phraseFor = (tools: EpisodeTool[], budget?: number): string => {
  const rule = ruleFor(tools[0]!.name)
  const verb = rule.verb

  if (tools.length === 1) {
    const only = tools[0]!
    const t = target(only, budget)
    const base = t ? `${verb} ${t}` : verb || titleName(only.name)
    const stat = only.added != null || only.removed != null ? ` (+${only.added ?? 0} -${only.removed ?? 0})` : ''

    return `${base}${stat}`
  }

  const unit = rule.unit || 'calls'

  return `${verb} ${tools.length} ${unit}`
}

// Left-clip so a long path keeps its meaningful tail (basename) instead of the
// truncate-end losing it: "…/controller/memory/agent_memory.go".
const clipPath = (s: string, n = 60): string => {
  const one = s.replace(/\s+/g, ' ').trim()

  return one.length > n ? `…${one.slice(one.length - (n - 1))}` : one
}

// Verb + detail split so a tool row can weight them differently (verb normal,
// "(detail)" dim) instead of one flat same-color string. Unlike the collapsed
// label (which uses the basename), the expanded row shows the fuller argument
// (full path / command) so drilling in restores the detail: read
// (…/memory/agent_memory.go), ran (ls -la /tmp/…), edited (notes.md +12 -3).
// The one-line label for a single call, used by every row that names one call.
// Deliberately SHORT: the full argument belongs to `toolArgument`, which the
// expanded detail block renders in full. A row is a title, not a payload.
export const toolParts = (tool: EpisodeTool): { verb: string; detail: string } => {
  const verb = ruleFor(tool.name).verb
  const raw = tool.summary.trim()
  const stat = tool.added != null || tool.removed != null ? `+${tool.added ?? 0} -${tool.removed ?? 0}` : ''
  const arg = PATHY.has(tool.name) && !tool.intent ? clipPath(raw, 60) : target(tool, 60)
  const detail = [arg, stat].filter(Boolean).join(' ')

  return { verb: verb || titleName(tool.name), detail }
}

// The full argument, for the expanded detail block: the whole command, the whole
// URL, the whole path. Never clipped here -- the block wraps it.
export const toolArgument = (tool: EpisodeTool): string => tool.summary.trim()

// The backend reports some failures as an "Error: ..." result rather than as a
// failed call (ToolRegistry.execute checks the very same prefix before adding
// its hint). Mirroring that here is what keeps such a call red instead of
// silently reading as success.
export const callFailed = (tool: EpisodeTool): boolean =>
  !tool.ok || /^error\b/i.test((tool.resultPreview ?? '').trimStart())

// What a folded block says about its failures. One failure is named, because
// knowing *which* call broke is the whole point of not auto-expanding; several
// only fit as a count.
export const failureNote = (tools: EpisodeTool[]): string => {
  const failed = tools.filter(callFailed)

  if (!failed.length) {
    return ''
  }

  if (failed.length > 1) {
    return `${failed.length} failed`
  }

  const { verb, detail } = toolParts(failed[0]!)

  return `${detail || verb} failed`
}

// Splits an episode's tools into consecutive same-name runs, e.g.
// [read, read, exec, read] -> [[read, read], [exec], [read]]. Used both to
// summarize a collapsed step and to render a run of N same-tool calls as a tree.
export const groupTools = (tools: EpisodeTool[]): EpisodeTool[][] => {
  const groups: EpisodeTool[][] = []

  for (const tool of tools) {
    const last = groups.at(-1)

    if (last && last[0]!.name === tool.name) {
      last.push(tool)
    } else {
      groups.push([tool])
    }
  }

  return groups
}

// How many result rows a tool shows before the rest folds behind a "+N" row.
// The view and the height estimator both read this; a mismatch reserves the
// wrong number of rows and leaves stale cells in the transcript.
//
// Every settled card opens at this height now, so it is the whole transcript's
// density knob rather than one card's: raise it and every stretch of work grows.
// Seven fits an ordinary listing or a short traceback without the "+N" row, and
// still leaves the turns around a card readable.
export const TOOL_PREVIEW_ROWS = 7

// Every line of a call's result, for the detail block. Nothing is filtered for
// being "redundant with the row above": a row carries a short label and the
// block is the one place the raw output appears, so dropping a payload here
// leaves the reader with an empty box and no way to see what came back.
//
// Relative indentation survives. Trimming each line was harmless while a result
// reached the client clipped to a row's worth, where there was no structure left
// to read; a card that shows a whole traceback or a stretch of source is the one
// place that structure is the content. The shared prefix still comes off, so an
// entirely indented payload does not spend the block's width on nothing.
export const previewLines = (tool: EpisodeTool): string[] => {
  const raw = tool.resultPreview ?? ''

  if (!raw) {
    return []
  }

  const kept = raw
    .split('\n')
    .map(l => l.trimEnd())
    // A read_file preview keeps source line numbers, so a blank source line
    // arrives as a non-empty "12|" that a plain emptiness check can't drop.
    .filter(l => l.trim() && !/^\s*\d+\|\s*$/.test(l))

  const shared = kept.reduce((n, l) => Math.min(n, l.length - l.trimStart().length), Infinity)

  return Number.isFinite(shared) && shared > 0 ? kept.map(l => l.slice(shared)) : kept
}

// Rows the preview occupies. `dense` is the inside-an-expanded-run view, where
// every result is squeezed onto one line so the run reads as a list of calls
// rather than a wall of output.
export const foldedPreviewRows = (tool: EpisodeTool, dense = false): number => {
  const n = previewLines(tool).length

  if (dense) {
    return n > 0 ? 1 : 0
  }

  return n > TOOL_PREVIEW_ROWS ? TOOL_PREVIEW_ROWS + 1 : n
}

// The ceiling on a fully expanded card. `previewLines` has no bound of its own:
// what reaches the client is a character budget, and a result that spends all of
// it on newlines would otherwise open to as many rows. High enough that an
// ordinary traceback or file read opens whole, which is the point of the level;
// low enough that one call cannot bury the turns around it.
export const TOOL_FULL_ROWS = 200

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

/**
 * The row under a block's output, which moves it between levels.
 *
 * `count` is what the click is worth -- rows it reveals, or rows it takes back.
 * A level with nothing to offer has no row at all, so both the view and the
 * height estimator ask this rather than re-deriving it from `hidden`.
 */
export interface MoreRow {
  count: number
  kind: 'collapse' | 'reveal'
}

/** What a call's detail block draws. `hidden` is the rows the level left behind. */
export interface DetailBlockShape {
  argument: string
  hidden: number
  more: MoreRow | null
  output: string[]
}

/**
 * The block a call's card renders, or `null` when it renders none.
 *
 * One source for the view and the height estimator. Deciding it twice is how an
 * estimate comes to disagree with what is drawn, and a disagreement here is the
 * stale-cell symptom the estimator is careful about everywhere else.
 *
 * The block repeats the row only when the row is showing the same thing -- the
 * argument modulo a display transform (a stripped scheme, quotes). A containment
 * test is too loose: `ruff check` is a prefix of `ruff check raven/ ui-tui/` and
 * would have swallowed a real argument. Suppressed only when there is output to
 * show in its place: a call still running has none, and dropping the argument as
 * well is what opened an empty slab on the one call a reader most wants to look
 * inside.
 */
export const detailBlockShape = (tool: EpisodeTool, full = false): DetailBlockShape | null => {
  const lines = previewLines(tool)
  const cap = full ? TOOL_FULL_ROWS : TOOL_PREVIEW_ROWS
  const output = lines.length > cap ? lines.slice(0, cap) : lines
  const rowDetail = toolParts(tool).detail.replace(/^"|"$/g, '')
  const argument = toolArgument(tool)
  const echoed = Boolean(rowDetail) && output.length > 0 && sameArgument(rowDetail, argument)
  const body = echoed ? '' : argument

  if (!body && output.length === 0) {
    return null
  }

  const hidden = lines.length - output.length
  // Revealed, the row turns around: it is what folds the card back to the cap.
  // Collapsing through the block's own click instead would shut the card
  // entirely, which is a level further than a reader who opened it wants to go.
  const more: MoreRow | null =
    !full && hidden > 0
      ? { count: hidden, kind: 'reveal' }
      : full && output.length > TOOL_PREVIEW_ROWS
        ? { count: output.length - TOOL_PREVIEW_ROWS, kind: 'collapse' }
        : null

  return { argument: body, hidden, more, output }
}

/**
 * Whether a settled call's card opens without being asked.
 *
 * Anything that came back earns the space. Length is not the discriminator it
 * once was: the card caps at `TOOL_PREVIEW_ROWS` and hands the remainder to a
 * "+N" row of its own, so a thousand-line result costs the transcript the same
 * six rows a five-line one does. Gating on the whole result fitting the cap left
 * exactly the long calls -- a directory listing, a file read -- as the ones a
 * reader had to open by hand, which is backwards.
 *
 * A call that returned nothing still does not open. Its block would hold the
 * argument alone, which the row above is already showing, so it opens four rows
 * to repeat one. A failure opens regardless: there the argument is what the
 * reader needs, and the row truncates it.
 *
 * A reader's own decision still wins: `foldStore` holds closed ids as well as
 * open ones precisely so a default cannot override it.
 */
export const cardDefaultOpen = (tool: EpisodeTool): boolean =>
  callFailed(tool) || Boolean(tool.diff) || previewLines(tool).length > 0

// Groups an episode's tools by name (consecutive runs) and joins the phrases:
// "read 6 files, ran ls, edited notes.md". `budget` is the row's own width; it
// only reaches a step whose entire activity is one target-style call, which has
// the row to itself. Phrases that must share a row keep the tight default, or
// a long first phrase would push the later ones off the end.
export const toolsSummary = (tools: EpisodeTool[], budget?: number): string => {
  const groups = groupTools(tools)

  if (!budget) {
    return groups.map(g => phraseFor(g)).join(', ')
  }

  // Share the row between the phrases instead of letting the first one spend it
  // all: three calls at the full budget each ran off the edge and the row ended
  // in a bare ", …". 2 = the ", " each phrase after the first costs.
  const share = Math.max(16, Math.floor(budget / groups.length) - 2)

  return groups.map(g => phraseFor(g, share)).join(', ')
}

// Whether any tool in the episode failed (drives the error tint on the label).
export const episodeFailed = (ep: Episode): boolean => ep.tools.some(t => !t.ok)

// The phrase a finished run of same-name calls collapses to: "read 6 files",
// "fetched 2 urls". Same producer as the collapsed step label, so a folded group
// and a folded step never describe the same work with different words.
export const toolsPhrase = (tools: EpisodeTool[]): string => phraseFor(tools)

export const failedCount = (tools: EpisodeTool[]): number => tools.filter(callFailed).length

// Undefined when nothing reported a duration, so the caller can omit the suffix
// instead of printing a confident "0s".
export const totalDurationMs = (tools: EpisodeTool[]): number | undefined => {
  const timed = tools.filter(t => t.durationMs != null)

  return timed.length ? timed.reduce((sum, t) => sum + t.durationMs!, 0) : undefined
}

// A turn reads as an alternating stream: the model speaks, the machine works,
// the model speaks again. `talk` carries one episode's reasoning + narration;
// `work` carries every call made between two things the model said, which is
// what a reader treats as "one stretch of work" -- so it spans episode
// boundaries. This replaces the old run/group split: one construct, not three.
export type Segment =
  | { episode: Episode; kind: 'talk' }
  | { key: string; kind: 'work'; live: boolean; tools: EpisodeTool[] }

export const segmentTurn = (episodes: Episode[], liveIndex?: number): Segment[] => {
  const segments: Segment[] = []
  let pending: EpisodeTool[] = []
  let pendingLive = false

  const flush = () => {
    if (pending.length) {
      segments.push({ key: pending[0]!.id, kind: 'work', live: pendingLive, tools: pending })
    }

    pending = []
    pendingLive = false
  }

  for (const ep of episodes) {
    const speaks =
      Boolean((ep.narration ?? '').trim()) ||
      Boolean(ep.steer) ||
      hasMeaningfulReasoning((ep.reasoning ?? '').trim())

    // Anything the model said closes the stretch of work before it.
    if (speaks) {
      flush()
      segments.push({ episode: ep, kind: 'talk' })
    }

    pending.push(...ep.tools)
    pendingLive = pendingLive || ep.index === liveIndex
  }

  flush()

  return segments
}
