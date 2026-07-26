// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import type { Episode, EpisodeTool } from '../types.js'

// Per-tool phrasing. `style` decides how a run of the same tool collapses:
//   count  — homogeneous info tools -> "read 6 files"; single -> the target
//   target — heterogeneous/mutating tools -> show the target; many -> "ran 3 commands"
// Verbs are deliberately Raven's own plain lowercase (not Claude Code's labels).
interface VerbRule {
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

// The rule for any tool: its override if we have one, else a generic rule built
// from the humanized name. This is what keeps the table from needing a row per
// tool — unknown tools get a real label ("image generate"), not a wrong "ran".
const ruleFor = (name: string): VerbRule =>
  OVERRIDES[name] ?? { verb: humanize(name) || name, unit: 'calls', style: 'target' }

// Search-like tools read better with the needle quoted: searched "DeviceFlow".
const QUOTED = new Set(['grep', 'find', 'web_search'])

// Only path-like arguments are clipped from the LEFT (a path's meaning lives in
// its tail). Questions, commands and prose must keep their head, or the row
// becomes unreadable ("…体是指哪个？").
const PATHY = new Set(['read_file', 'write_file', 'edit_file', 'list_dir'])

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

// The visible target for a single call: a file basename for path-like tools, a
// quoted needle for search tools, else the trimmed argument itself.
const target = (tool: EpisodeTool): string => {
  const raw = tool.summary.trim()

  if (!raw) {
    return ''
  }

  if (
    tool.name === 'read_file' ||
    tool.name === 'write_file' ||
    tool.name === 'edit_file' ||
    tool.name === 'list_dir'
  ) {
    return clip(raw.split(/[\\/]/).pop() || raw)
  }

  if (QUOTED.has(tool.name)) {
    return `"${clip(raw, 32)}"`
  }

  return clip(raw)
}

const phraseFor = (tools: EpisodeTool[]): string => {
  const rule = ruleFor(tools[0]!.name)
  const verb = rule.verb

  if (tools.length === 1) {
    const only = tools[0]!
    const t = target(only)
    const base = t ? `${verb} ${t}` : verb || titleName(only.name)
    const stat = only.added != null || only.removed != null ? ` (+${only.added ?? 0} -${only.removed ?? 0})` : ''

    return `${base}${stat}`
  }

  const unit = rule.unit || 'calls'

  return `${verb} ${tools.length} ${unit}`
}

// A single tool's expanded-row phrase: "read approve.go", "ran ls biz/".
export const toolLine = (tool: EpisodeTool): string => phraseFor([tool])

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
export const toolParts = (tool: EpisodeTool): { verb: string; detail: string } => {
  const verb = ruleFor(tool.name).verb
  const raw = tool.summary.trim()
  const stat = tool.added != null || tool.removed != null ? `+${tool.added ?? 0} -${tool.removed ?? 0}` : ''
  // Budget is generous: the row itself truncates to the terminal width, so
  // pre-clipping only guards against pathological one-liners.
  const arg = QUOTED.has(tool.name)
    ? raw
      ? `"${clip(raw, 120)}"`
      : ''
    : PATHY.has(tool.name)
      ? clipPath(raw, 120)
      : clip(raw, 120)
  const detail = [arg, stat].filter(Boolean).join(' ')

  return { verb: verb || titleName(tool.name), detail }
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

// Groups an episode's tools by name (consecutive runs) and joins the phrases:
// "read 6 files, ran ls, edited notes.md".
export const toolsSummary = (tools: EpisodeTool[]): string => groupTools(tools).map(phraseFor).join(', ')

// Flatten inline markdown to plain text for the one-line label: the model's
// narration often contains **bold**, `code`, ## headings, or | tables that
// would otherwise show literally in the collapsed row.
const stripMd = (s: string) =>
  s
    .replace(/[*_`~]/g, '')
    .replace(/^#{1,6}\s*/gm, '')
    .replace(/\|/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()

// The one-line label for a collapsed episode: its narration if the model spoke,
// otherwise the tool summary. Empty when the step did nothing describable.
export const episodeLabel = (ep: Episode): string => {
  const narration = stripMd(ep.narration ?? '')

  if (narration) {
    return clip(narration, 72)
  }

  const tools = toolsSummary(ep.tools)

  if (tools) {
    return tools
  }

  // A reasoning-only step (kept for its thinking) still gets a readable label.
  const reasoning = stripMd(ep.reasoning ?? '')

  return reasoning ? clip(reasoning, 72) : ''
}

// Whether any tool in the episode failed (drives the error tint on the label).
export const episodeFailed = (ep: Episode): boolean => ep.tools.some(t => !t.ok)

// Turn-level one-liner: "20 steps · 2m04s — read 12 files, ran 4 commands, 1 failed".
// Clipped so the collapsed turn stays a single line even on a busy turn.
export const turnSummary = (episodes: Episode[]): string => {
  const allTools = episodes.flatMap(ep => ep.tools)
  const parts = [toolsSummary(allTools)].filter(Boolean)
  const failed = allTools.filter(t => !t.ok).length

  if (failed) {
    parts.push(`${failed} failed`)
  }

  return clip(parts.join(', '), 90)
}
