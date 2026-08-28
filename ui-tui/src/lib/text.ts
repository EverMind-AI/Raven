// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { stringWidth } from '@hermes/ink'

import type { ThinkingMode } from '../types.js'

import {
  HISTORY_RENDER_MAX_CHARS,
  HISTORY_RENDER_MAX_LINES,
  LIVE_RENDER_MAX_CHARS,
  LIVE_RENDER_MAX_LINES,
  THINKING_COT_MAX
} from '../config/limits.js'
import { FACES } from '../content/faces.js'
import { VERBS } from '../content/verbs.js'

const ESC = String.fromCharCode(27)
const ANSI_RE = new RegExp(`${ESC}\\[[0-9;]*m`, 'g')
const WS_RE = /\s+/g

export const stripAnsi = (s: string) => s.replace(ANSI_RE, '')

export const hasAnsi = (s: string) => s.includes(`${ESC}[`) || s.includes(`${ESC}]`)

const renderEstimateLine = (line: string) => {
  const trimmed = line.trim()

  if (trimmed.startsWith('|')) {
    return trimmed
      .split('|')
      .filter(Boolean)
      .map(cell => cell.trim())
      .join('  ')
  }

  return line
    .replace(/!\[(.*?)\]\(([^)\s]+)\)/g, '[image: $1]')
    .replace(/\[(.+?)\]\((https?:\/\/[^\s)]+)\)/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/(?<!\w)__(.+?)__(?!\w)/g, '$1')
    .replace(/\*(.+?)\*/g, '$1')
    .replace(/(?<!\w)_(.+?)_(?!\w)/g, '$1')
    .replace(/~~(.+?)~~/g, '$1')
    .replace(/==(.+?)==/g, '$1')
    .replace(/\[\^([^\]]+)\]/g, '[$1]')
    .replace(/^#{1,6}\s+/, '')
    .replace(/^\s*[-*+]\s+\[( |x|X)\]\s+/, (_m, checked: string) => `• [${checked.toLowerCase() === 'x' ? 'x' : ' '}] `)
    .replace(/^\s*[-*+]\s+/, '• ')
    .replace(/^\s*(\d+)\.\s+/, '$1. ')
    .replace(/^\s*(?:>\s*)+/, '│ ')
}

export const compactPreview = (s: string, max: number) => {
  const one = s.replace(WS_RE, ' ').trim()

  return !one ? '' : one.length > max ? one.slice(0, max - 1) + '…' : one
}

// Clip to a display-cell budget (CJK/emoji aware). Needed wherever a row must
// fit a known column count and the surrounding markup nests <Text> (a nested
// Text makes ink's own `truncate-end` a no-op, so the text would overflow).
export const clipToWidth = (raw: string, width: number) => {
  const one = raw.replace(WS_RE, ' ').trim()

  if (width <= 0 || stringWidth(one) <= width) {
    return one
  }

  let out = ''
  let w = 0

  for (const ch of one) {
    const cw = stringWidth(ch)

    if (w + cw > width - 1) {
      break
    }

    out += ch
    w += cw
  }

  return `${out}…`
}

// The mirror of `clipToWidth`: keep the *end* of a string inside a display-cell
// budget. A live tail grows at the right, so the row has to shed cells from the
// left to stay on one line -- slicing by character instead would cut a CJK tail
// to half the columns it was given.
export const clipToWidthFromEnd = (raw: string, width: number) => {
  const one = raw.replace(WS_RE, ' ').trim()

  if (width <= 0) {
    return ''
  }

  if (stringWidth(one) <= width) {
    return one
  }

  const chars = [...one]
  let out = ''
  let w = 0

  for (let i = chars.length - 1; i >= 0; i--) {
    const ch = chars[i]!
    const cw = stringWidth(ch)

    if (w + cw > width - 1) {
      break
    }

    out = ch + out
    w += cw
  }

  // The cut often lands on a space, and `… bbbb` spends a cell saying nothing.
  return `…${out.replace(/^ /, '')}`
}

const headCells = (chars: readonly string[], budget: number) => {
  let out = ''
  let w = 0

  for (const ch of chars) {
    const cw = stringWidth(ch)

    if (w + cw > budget) {
      break
    }

    out += ch
    w += cw
  }

  return out
}

const tailCells = (chars: readonly string[], budget: number) => {
  let out = ''
  let w = 0

  for (let i = chars.length - 1; i >= 0; i--) {
    const cw = stringWidth(chars[i]!)

    if (w + cw > budget) {
      break
    }

    out = chars[i]! + out
    w += cw
  }

  return out
}

// Keep both ends of an identifier inside a display-cell budget. A generated id
// is recognisable by its head and its tail and by nothing in between: clipping
// from the right leaves two runs of the same session looking identical for
// twenty cells, which is worse than showing less of each.
export const elideMiddle = (raw: string, width: number) => {
  const one = raw.trim()

  if (width <= 1 || stringWidth(one) <= width) {
    return one
  }

  const chars = [...one]
  const keep = width - 1
  const head = Math.ceil(keep / 2)

  return `${headCells(chars, head)}\u2026${tailCells(chars, keep - head)}`
}

// Pad to a display-cell width, clipping anything that overruns it. Used for the
// row columns a reader scans down: `padEnd` counts code points, so one CJK name
// in the column knocks every row below it out of alignment.
export const padToWidth = (raw: string, width: number) => {
  const clipped = stringWidth(raw) > width ? clipToWidth(raw, width) : raw

  return clipped + ' '.repeat(Math.max(0, width - stringWidth(clipped)))
}

export const TOOL_RESULT_PREVIEW_CHARS = 200

// A tool's inline result preview. Cutting on a raw character budget alone split
// the last line mid-token ("updat" for "updated", a table header whose body
// never arrives), so drop that partial line instead. A preview that is one long
// line has nowhere to break and still gets the ellipsis treatment.
export const toolResultPreview = (raw: string, max = TOOL_RESULT_PREVIEW_CHARS) => {
  const text = raw.trimEnd()

  if (text.length <= max) {
    return text
  }

  const lastBreak = text.slice(0, max).lastIndexOf('\n')

  return lastBreak > 0 ? text.slice(0, lastBreak) : compactPreview(text, max)
}

// Like compactPreview but keeps the tail — for live-streaming text (reasoning)
// where the most recent tokens matter, so the view follows the stream instead
// of freezing on the first `max` chars.
export const tailPreview = (s: string, max: number) => {
  const one = s.replace(WS_RE, ' ').trim()

  return !one ? '' : one.length > max ? '…' + one.slice(one.length - (max - 1)) : one
}

export const estimateTokensRough = (text: string) => (!text ? 0 : (text.length + 3) >> 2)

export const edgePreview = (s: string, head = 16, tail = 28) => {
  const one = s.replace(WS_RE, ' ').trim().replace(/\]\]/g, '] ]')

  return !one
    ? ''
    : one.length <= head + tail + 4
      ? one
      : `${one.slice(0, head).trimEnd()}.. ${one.slice(-tail).trimStart()}`
}

export const pasteTokenLabel = (text: string, lineCount: number) => {
  const preview = edgePreview(text)

  if (!preview) {
    return `[[ [${fmtK(lineCount)} lines] ]]`
  }

  const [head = preview, tail = ''] = preview.split('.. ', 2)

  return tail
    ? `[[ ${head.trimEnd()}.. [${fmtK(lineCount)} lines] .. ${tail.trimStart()} ]]`
    : `[[ ${preview} [${fmtK(lineCount)} lines] ]]`
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

const THINKING_STATUS_RE = new RegExp(`^(?:${VERBS.join('|')})\\.{0,3}$`, 'i')
// The status ticker renders one FACES glyph immediately followed by one VERBS
// word (appChrome's FaceTicker), so that pair -- not "any run of non-letters"
// -- is the leak's signature. Matching the faces literally keeps every
// quantifier bounded: the earlier `[^A-Za-z\n]+` stand-in for a face matches a
// whole line of non-Latin text, which is quadratic to backtrack and swallowed
// the prose in front of any verb-like word. A bare verb alone on a line is
// THINKING_STATUS_RE's job, not this one's.
const THINKING_STATUS_CHUNK_RE = new RegExp(
  `(?:${FACES.map(escapeRe).join('|')})[ \\t]*(?:${VERBS.join('|')})\\.{0,3}[ \\t]*`,
  'giu'
)

export const cleanThinkingText = (reasoning: string) =>
  reasoning
    .split('\n')
    .map(line => line.replace(THINKING_STATUS_CHUNK_RE, '').trim())
    .filter(line => line && !THINKING_STATUS_RE.test(line.replace(/\.\.\.$/, '').trim()))
    .join('\n')
    .replace(/([^\n])(?=\*\*[^*\n][^\n]*?\*\*)/g, '$1\n\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()

// `max` bounds the one-line `truncated` preview only; `full` renders the cleaned
// text whole and its caller bounds what reaches the screen.
export const thinkingPreview = (reasoning: string, mode: ThinkingMode, max: number = THINKING_COT_MAX) => {
  // Ahead of the clean, not after it: a collapsed section shows nothing, and
  // this runs on every streamed reasoning update.
  if (mode === 'collapsed') {
    return ''
  }

  const raw = cleanThinkingText(reasoning)

  return !raw ? '' : mode === 'full' ? raw : compactPreview(raw.replace(WS_RE, ' '), max)
}

export const boundedLiveRenderText = (
  text: string,
  { maxChars = LIVE_RENDER_MAX_CHARS, maxLines = LIVE_RENDER_MAX_LINES } = {}
) => boundedRenderText(text, 'showing live tail', { maxChars, maxLines })

export const boundedHistoryRenderText = (
  text: string,
  { maxChars = HISTORY_RENDER_MAX_CHARS, maxLines = HISTORY_RENDER_MAX_LINES } = {}
) => boundedRenderText(text, 'showing tail', { maxChars, maxLines })

const boundedRenderText = (
  text: string,
  labelPrefix: string,
  { maxChars, maxLines }: { maxChars: number; maxLines: number }
) => {
  if (text.length <= maxChars && text.split('\n', maxLines + 1).length <= maxLines) {
    return text
  }

  let start = 0
  let idx = text.length

  for (let seen = 0; seen < maxLines && idx > 0; seen++) {
    idx = text.lastIndexOf('\n', idx - 1)
    start = idx < 0 ? 0 : idx + 1

    if (idx < 0) {
      break
    }
  }

  const lineStart = start
  start = Math.max(lineStart, text.length - maxChars)

  if (start > lineStart) {
    const nextBreak = text.indexOf('\n', start)

    if (nextBreak >= 0 && nextBreak < text.length - 1) {
      start = nextBreak + 1
    }
  }

  const tail = text.slice(start).trimStart()
  const omittedLines = countNewlines(text, start)
  const omittedChars = Math.max(0, text.length - tail.length)

  const label =
    omittedLines > 0
      ? `[${labelPrefix}; omitted ${fmtK(omittedLines)} lines / ${fmtK(omittedChars)} chars]\n`
      : `[${labelPrefix}; omitted ${fmtK(omittedChars)} chars]\n`

  return `${label}${tail}`
}

const countNewlines = (text: string, end: number) => {
  let count = 0

  for (let i = 0; i < end; i++) {
    if (text.charCodeAt(i) === 10) {
      count++
    }
  }

  return count
}

export const stripTrailingPasteNewlines = (text: string) => (/[^\n]/.test(text) ? text.replace(/\n+$/, '') : text)

export const toolTrailLabel = (name: string) =>
  name
    .split('_')
    .filter(Boolean)
    .map(p => p[0]!.toUpperCase() + p.slice(1))
    .join(' ') || name

export const formatToolCall = (name: string, context = '') => {
  const label = toolTrailLabel(name)
  const preview = compactPreview(context, 64)

  return preview ? `${label}("${preview}")` : label
}

export const buildToolTrailLine = (
  name: string,
  context: string,
  error?: boolean,
  note?: string,
  duration?: number
) => {
  const detail = compactPreview(note ?? '', 72)
  const took = duration !== undefined ? ` (${duration.toFixed(1)}s)` : ''

  return `${formatToolCall(name, context)}${took}${detail ? ` :: ${detail}` : ''} ${error ? '✗' : '✓'}`
}

export const isToolTrailResultLine = (line: string) => line.endsWith(' ✓') || line.endsWith(' ✗')

export const parseToolTrailResultLine = (line: string) => {
  if (!isToolTrailResultLine(line)) {
    return null
  }

  const mark = line.endsWith(' ✗') ? '✗' : '✓'
  const body = line.slice(0, -2)
  const [call, detail] = body.split(' :: ', 2)

  if (detail != null) {
    return { call, detail, mark }
  }

  const legacy = body.indexOf(': ')

  if (legacy > 0) {
    return { call: body.slice(0, legacy), detail: body.slice(legacy + 2), mark }
  }

  return { call: body, detail: '', mark }
}

export const splitToolDuration = (call: string) => {
  const match = call.match(/^(.*?)( \(\d+(?:\.\d)?s\))$/)

  return match ? { label: match[1]!, duration: match[2]! } : { label: call, duration: '' }
}

export const isTransientTrailLine = (line: string) => line.startsWith('drafting ') || line === 'analyzing tool output…'

export const sameToolTrailGroup = (label: string, entry: string) =>
  entry === `${label} ✓` ||
  entry === `${label} ✗` ||
  entry.startsWith(`${label}(`) ||
  entry.startsWith(`${label} ::`) ||
  entry.startsWith(`${label}:`)

export const lastCotTrailIndex = (trail: readonly string[]) => {
  for (let i = trail.length - 1; i >= 0; i--) {
    if (!isToolTrailResultLine(trail[i]!)) {
      return i
    }
  }

  return -1
}

export const estimateRows = (text: string, w: number, compact = false) => {
  let fence: { char: '`' | '~'; len: number } | null = null
  let rows = 0

  for (const raw of text.split('\n')) {
    const line = stripAnsi(raw)
    const maybeFence = line.match(/^\s*(`{3,}|~{3,})(.*)$/)

    if (maybeFence) {
      const marker = maybeFence[1]!
      const lang = maybeFence[2]!.trim()

      if (!fence) {
        fence = { char: marker[0] as '`' | '~', len: marker.length }

        if (lang) {
          rows += Math.ceil((`─ ${lang}`.length || 1) / w)
        }
      } else if (marker[0] === fence.char && marker.length >= fence.len) {
        fence = null
      }

      continue
    }

    const inCode = Boolean(fence)
    const trimmed = line.trim()

    if (!inCode && trimmed.startsWith('|') && /^[|\s:-]+$/.test(trimmed)) {
      continue
    }

    const rendered = inCode ? line : renderEstimateLine(line)

    if (compact && !rendered.trim()) {
      continue
    }

    rows += Math.ceil((rendered.length || 1) / w)
  }

  return Math.max(1, rows)
}

export const flat = (r: Record<string, string[]>) => Object.values(r).flat()

const COMPACT_NUMBER = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1, notation: 'compact' })

export const fmtK = (n: number) => COMPACT_NUMBER.format(n).replace(/[KMBT]$/, s => s.toLowerCase())

export const pick = <T>(a: T[]) => a[Math.floor(Math.random() * a.length)]!

export const isPasteBackedText = (text: string) =>
  /\[\[paste:\d+(?:[^\n]*?)\]\]|\[paste #\d+ (?:attached|excerpt)(?:[^\n]*?)\]/.test(text)
