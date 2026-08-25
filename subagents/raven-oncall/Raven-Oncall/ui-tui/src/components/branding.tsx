// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { Box, Text, useStdout } from '@hermes/ink'
import { useEffect, useState } from 'react'
import unicodeSpinners from 'unicode-animations'

import type { PanelSection, SessionInfo } from '../types.js'

import {
  ravenHero,
  RAVEN_HERO_WIDTH,
  RAVEN_LOGO_WIDTH,
  ravenLogo,
  ravenLogoWord,
  RAVEN_WORD_WIDTH,
  rowsWidth
} from '../banner.js'
import { flat } from '../lib/text.js'
import { DEFAULT_THEME, type Theme } from '../theme.js'

const LOADER_TICK_MS = 120

function InlineLoader({ label, t }: { label: string; t: Theme }) {
  const [tick, setTick] = useState(0)
  const spinner = unicodeSpinners.braille
  const frame = spinner.frames[tick % spinner.frames.length] ?? '⠋'

  useEffect(() => {
    const id = setInterval(() => setTick(n => n + 1), Math.max(LOADER_TICK_MS, spinner.interval))

    return () => clearInterval(id)
  }, [spinner.interval])

  return (
    <Text color={t.color.muted} wrap="truncate">
      <Text color={t.color.accent}>{frame}</Text> {label}
    </Text>
  )
}

const STARTUP_MESSAGES = ['summoning raven…', 'building agent loop…', 'loading tools & skills…']
const STARTUP_LABEL_MS = 900

// Placeholder shown in the intro row while the backend builds the agent loop,
// before the session.info handshake populates SessionPanel. Wrapped in the same
// rounded box as SessionPanel so the handoff reads as one region's content
// changing rather than a block appearing from nowhere.
export function StartupLoader({ t }: { t: Theme }) {
  const [step, setStep] = useState(0)

  useEffect(() => {
    const id = setInterval(() => setStep(n => n + 1), STARTUP_LABEL_MS)

    return () => clearInterval(id)
  }, [])

  const label = STARTUP_MESSAGES[Math.min(step, STARTUP_MESSAGES.length - 1)] ?? STARTUP_MESSAGES[0]

  return (
    <Box borderColor={t.color.border} borderStyle="round" marginBottom={1} paddingX={2} paddingY={1}>
      <InlineLoader label={label} t={t} />
    </Box>
  )
}

export function ArtLines({ lines }: { lines: [string, string][] }) {
  return (
    <>
      {lines.map(([c, text], i) => (
        // `truncate` so wide banner art clips at the box edge instead of
        // wrapping each row into an unreadable scatter on narrow terminals.
        <Text color={c} key={i} wrap="truncate">
          {text}
        </Text>
      ))}
    </>
  )
}

// Like ArtLines but each row is an array of `[color, segment]` pairs rendered
// inline — used for horizontally-graded art (the raven hero).
export function ArtRows({ rows }: { rows: [string, string][][] }) {
  return (
    <>
      {rows.map((segs, i) => (
        <Text key={i}>
          {segs.map(([c, text], j) => (
            <Text color={c} key={j}>
              {text}
            </Text>
          ))}
        </Text>
      ))}
    </>
  )
}

const PROVIDER_LABELS: Record<string, string> = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  openrouter: 'OpenRouter',
  qwen: 'Qwen',
  google: 'Google',
  mistral: 'Mistral'
}

// formatProvider — Resolve a user-facing provider label.
//
// Why the slug → model_id fallback: user config may set `provider="auto"`
// (LiteLLM auto-routing dispatch mode), which isn't a real provider name.
// In that case parse the model_id prefix (e.g. "openrouter/qwen/..." →
// "openrouter") to find the real provider.
//
// Why the LUT: capitalize-only would yield "Openai" / "Openrouter" — visually
// wrong. PROVIDER_LABELS keeps canonical casing for known providers; unknown
// providers fall back to plain capitalize.
export function formatProvider(slug?: string, modelId?: string): string {
  let effective = slug ?? ''
  if (!effective || effective === 'auto') {
    // Only treat model_id as carrying provider info when it has a `/` prefix
    // (e.g. "openrouter/qwen/qwen3.6-plus"). A bare "sonnet" is a model name,
    // not a provider — fall through to '—'.
    const id = modelId ?? ''
    effective = id.includes('/') ? (id.split('/')[0] ?? '') : ''
  }
  if (!effective) {
    return '—'
  }
  const key = effective.toLowerCase()
  return PROVIDER_LABELS[key] ?? effective.charAt(0).toUpperCase() + effective.slice(1)
}

// The full single-line wordmark needs the terminal clearly wider than the art:
// RAVEN_LOGO_WIDTH plus the transcript chrome (paddingX + scrollbar gutter) and
// headroom — empirically a 144-col terminal still wraps the 136-col line.
const LOGO_FULL_MIN_COLS = RAVEN_LOGO_WIDTH + 12
// The "RAVEN"-only form needs just the first word's width, so it can show on
// much narrower terminals — the 68-col word plus a little breathing room.
const LOGO_WORD_MIN_COLS = RAVEN_WORD_WIDTH + 5

export function Branding({ t }: { t?: Theme } = {}) {
  const theme = t ?? DEFAULT_THEME
  const yellow = theme.yellow
  // Banner draws the .100/.300/.500/.600 stops (skip .50 so the top band isn't
  // near-white); ravenLogo maps one ramp entry per vertical band.
  const palette = [yellow[1], yellow[2], yellow[3], yellow[4]]
  const cols = useStdout().stdout?.columns ?? 80

  // Full single-line "RAVEN AGENT" when it fits; otherwise just the "RAVEN"
  // word; and only when even that won't fit, a compact one-line title.
  // ArtLines truncates so nothing ever wraps.
  if (cols >= LOGO_FULL_MIN_COLS) {
    return (
      <Box flexDirection="column" marginBottom={1}>
        <ArtLines lines={ravenLogo(palette)} />
      </Box>
    )
  }

  if (cols >= LOGO_WORD_MIN_COLS) {
    return (
      <Box flexDirection="column" marginBottom={1}>
        <ArtLines lines={ravenLogoWord(palette)} />
      </Box>
    )
  }

  return (
    <Box marginBottom={1}>
      <Text bold color={theme.color.primary}>
        {theme.brand.icon} {theme.brand.name}
      </Text>
    </Box>
  )
}

// Hermes-era callers (appLayout.tsx) import `Banner`; keep the name working.
export const Banner = Branding

// ── Collapsible helpers ──────────────────────────────────────────────

function CollapseToggle({
  count,
  open,
  suffix,
  t,
  title,
  onToggle
}: {
  count?: number
  open: boolean
  suffix?: string
  t: Theme
  title: string
  onToggle: () => void
}) {
  return (
    <Box onClick={onToggle}>
      <Text color={t.color.accent}>{open ? '▾ ' : '▸ '}</Text>
      <Text bold color={t.color.accent}>
        {title}
      </Text>
      {typeof count === 'number' ? <Text color={t.color.muted}> ({count})</Text> : null}
      {suffix ? <Text color={t.color.muted}> {suffix}</Text> : null}
    </Box>
  )
}

// ── SessionPanel ─────────────────────────────────────────────────────

const SKILLS_MAX = 8
const TOOLSETS_MAX = 8
const FOOTER_HELP_TEXT = '/help for commands'

export function SessionPanel({ info, maxCols, sid, t }: SessionPanelProps) {
  // Width the panel actually has. The full terminal width overshoots whenever
  // the panel is embedded in a narrower container (e.g. the demo gallery's
  // sidebar), so callers can pass `maxCols`; otherwise assume the terminal.
  const stdoutCols = useStdout().stdout?.columns ?? 100
  const cols = maxCols ?? stdoutCols
  // Hero is rendered as a solid brand color (no ramp gradient).
  const heroRamp = [t.color.primary, t.color.primary, t.color.primary, t.color.primary]
  const heroRows = ravenHero(heroRamp, t.bannerHero || undefined)
  const leftW = Math.min((rowsWidth(heroRows) || RAVEN_HERO_WIDTH) + 4, Math.floor(cols * 0.4))
  const wide = cols >= 90 && leftW + 40 < cols
  const w = Math.max(20, wide ? cols - leftW - 14 : cols - 12)
  const lineBudget = Math.max(12, w - 2)
  const strip = (s: string) => (s.endsWith('_tools') ? s.slice(0, -6) : s)

  // Footer meta (model · provider · session). Kept beside `/help` only when it
  // fits the column; otherwise the footer becomes a column so the whole meta
  // line drops below `/help` instead of wrapping mid-string.
  const footerMeta = `${info.model.split('/').pop()} · ${formatProvider(info.provider, info.model_id)}${sid ? ` · ${sid}` : ''}`
  const footerInline = FOOTER_HELP_TEXT.length + 2 + footerMeta.length <= w

  // ── Local collapse state for each section ──
  const [toolsOpen, setToolsOpen] = useState(true)
  const [skillsOpen, setSkillsOpen] = useState(false)
  const [systemOpen, setSystemOpen] = useState(false)
  const [mcpOpen, setMcpOpen] = useState(false)

  const truncLine = (pfx: string, items: string[]) => {
    let line = ''
    let shown = 0

    for (const item of [...items].sort()) {
      const next = line ? `${line}, ${item}` : item

      if (pfx.length + next.length > lineBudget) {
        return line ? `${line}, …+${items.length - shown}` : `${item}, …`
      }

      line = next
      shown++
    }

    return line
  }

  // ── Collapsible skills section ──
  const skillEntries = Object.entries(info.skills).sort()
  const skillsTotal = flat(info.skills).length
  const skillsCatCount = skillEntries.length

  const skillsBody = () => {
    if (info.lazy && skillEntries.length === 0) {
      return <InlineLoader label="scanning skills" t={t} />
    }

    const shown = skillEntries.slice(0, SKILLS_MAX)
    const overflow = skillEntries.length - SKILLS_MAX

    return (
      <>
        {shown.map(([k, vs]) => (
          <Text key={k} wrap="truncate">
            <Text color={t.color.muted}>{strip(k)}: </Text>
            <Text color={t.color.text}>{truncLine(strip(k) + ': ', vs)}</Text>
          </Text>
        ))}
        {overflow > 0 && <Text color={t.color.muted}>(and {overflow} more categories…)</Text>}
      </>
    )
  }

  // ── Collapsible tools section ──
  const toolEntries = Object.entries(info.tools).sort()
  const toolsTotal = flat(info.tools).length

  const toolsBody = () => {
    const shown = toolEntries.slice(0, TOOLSETS_MAX)
    const overflow = toolEntries.length - TOOLSETS_MAX

    return (
      <>
        {shown.map(([k, vs]) => (
          <Text key={k} wrap="truncate">
            <Text color={t.color.muted}>{strip(k)}: </Text>
            <Text color={t.color.text}>{truncLine(strip(k) + ': ', vs)}</Text>
          </Text>
        ))}
        {overflow > 0 && <Text color={t.color.muted}>(and {overflow} more toolsets…)</Text>}
      </>
    )
  }

  // ── Collapsible MCP section ──
  const mcpBody = () => (
    <>
      {(info.mcp_servers ?? []).map(s => (
        <Text key={s.name} wrap="truncate">
          <Text color={t.color.muted}>{`  ${s.name} `}</Text>
          <Text color={t.color.muted}>{`[${s.transport}]`}</Text>
          <Text color={t.color.muted}>: </Text>
          {s.connected ? (
            <Text color={t.color.text}>
              {s.tools} tool{s.tools === 1 ? '' : 's'}
            </Text>
          ) : (
            <Text color={t.color.error}>failed</Text>
          )}
        </Text>
      ))}
    </>
  )

  // ── System prompt body ──
  const sysPromptLen = (info.system_prompt ?? '').length

  const systemBody = () => {
    if (sysPromptLen === 0) {
      return <Text color={t.color.muted}>No system prompt loaded.</Text>
    }

    return <Text color={t.color.muted}>{info.system_prompt}</Text>
  }

  return (
    <Box borderColor={t.color.border} borderStyle="round" marginBottom={1} paddingX={2} paddingY={1}>
      {wide && (
        <Box flexDirection="column" marginRight={2} width={leftW}>
          <ArtRows rows={heroRows} />
        </Box>
      )}

      <Box flexDirection="column" width={w}>
        {/* Upper content grows to push the working-dir footer to the bottom of
            the panel when the (taller) hero stretches it. flexBasis stays auto
            so the box keeps its content height when there is no hero (narrow
            mode) — flexBasis={0} would collapse it to nothing there. */}
        <Box flexDirection="column" flexGrow={1} width={w}>
          <Box marginBottom={1}>
            <Text bold color={t.color.primary}>
              {t.brand.name}
              {info.version ? ` v${info.version}` : ''}
              {info.release_date ? ` (${info.release_date})` : ''}
            </Text>
          </Box>

          {/* ── Tools (expanded by default) ── */}
          <Box flexDirection="column" marginTop={1}>
            <CollapseToggle
              count={toolsTotal}
              onToggle={() => setToolsOpen(v => !v)}
              open={toolsOpen}
              t={t}
              title="Available Tools"
            />
            {toolsOpen && toolsBody()}
          </Box>

          {/* ── Skills (collapsed by default) ── */}
          <Box flexDirection="column" marginTop={1}>
            <CollapseToggle
              count={skillsTotal}
              onToggle={() => setSkillsOpen(v => !v)}
              open={skillsOpen}
              suffix={
                skillsCatCount > 0 ? `in ${skillsCatCount} categor${skillsCatCount === 1 ? 'y' : 'ies'}` : undefined
              }
              t={t}
              title="Available Skills"
            />
            {skillsOpen && skillsBody()}
          </Box>

          {/* ── System Prompt (collapsed by default) ── */}
          {sysPromptLen > 0 && (
            <Box flexDirection="column" marginTop={1}>
              <CollapseToggle
                onToggle={() => setSystemOpen(v => !v)}
                open={systemOpen}
                suffix={`— ${sysPromptLen.toLocaleString()} chars`}
                t={t}
                title="System Prompt"
              />
              {systemOpen && systemBody()}
            </Box>
          )}

          {/* ── MCP Servers (collapsed by default) ── */}
          {info.mcp_servers && info.mcp_servers.length > 0 && (
            <Box flexDirection="column" marginTop={1}>
              <CollapseToggle
                count={info.mcp_servers.length}
                onToggle={() => setMcpOpen(v => !v)}
                open={mcpOpen}
                suffix="connected"
                t={t}
                title="MCP Servers"
              />
              {mcpOpen && mcpBody()}
            </Box>
          )}

          {/* Divider above the footer. */}
          <Box marginTop={1}>
            <Text color={t.color.border} wrap="truncate">
              {'─'.repeat(Math.max(1, w))}
            </Text>
          </Box>

          {/* Footer: counts + /help on the left, model · session id on the right. */}
          <Box
            flexDirection={footerInline ? 'row' : 'column'}
            justifyContent={footerInline ? 'space-between' : 'flex-start'}
          >
            <Text color={t.color.muted}>
              <Text color={t.color.accent}>/help</Text> for commands
            </Text>

            <Text color={t.color.muted} wrap="wrap">
              {footerMeta}
            </Text>
          </Box>

          {typeof info.update_behind === 'number' && info.update_behind > 0 && (
            <>
              <Box>
                <Text color={t.color.border} wrap="truncate">
                  {'─'.repeat(Math.max(1, w))}
                </Text>
              </Box>
              <Text bold color={t.color.warn}>
                ! {info.update_behind} {info.update_behind === 1 ? 'commit' : 'commits'} behind
                <Text bold={false} color={t.color.warn} dimColor>
                  {' '}
                  - run{' '}
                </Text>
                <Text bold color={t.color.warn}>
                  {info.update_command || 'raven upgrade'}
                </Text>
                <Text bold={false} color={t.color.warn} dimColor>
                  {' '}
                  to update
                </Text>
              </Text>
            </>
          )}
        </Box>

        <Box marginTop={1}>
          <Text color={t.color.muted} wrap="truncate-end">
            {info.cwd || process.cwd()}
          </Text>
        </Box>
      </Box>
    </Box>
  )
}

export function Panel({ sections, t, title }: PanelProps) {
  return (
    <Box borderColor={t.color.border} borderStyle="round" flexDirection="column" paddingX={2} paddingY={1}>
      <Box justifyContent="center" marginBottom={1}>
        <Text bold color={t.color.primary}>
          {title}
        </Text>
      </Box>

      {sections.map((sec, si) => (
        <Box flexDirection="column" key={si} marginTop={si > 0 ? 1 : 0}>
          {sec.title && (
            <Text bold color={t.color.accent}>
              {sec.title}
            </Text>
          )}

          {sec.rows?.map(([k, v], ri) => (
            <Text key={ri} wrap="truncate">
              <Text color={t.color.muted}>{k.padEnd(20)}</Text>
              <Text color={t.color.text}>{v}</Text>
            </Text>
          ))}

          {sec.items?.map((item, ii) => (
            <Text color={t.color.text} key={ii} wrap="truncate">
              {item}
            </Text>
          ))}

          {sec.text && <Text color={t.color.muted}>{sec.text}</Text>}
        </Box>
      ))}
    </Box>
  )
}

interface PanelProps {
  sections: PanelSection[]
  t: Theme
  title: string
}

interface SessionPanelProps {
  info: SessionInfo
  // Container width to lay out against; defaults to the full terminal. Pass it
  // when embedding the panel in a narrower region (e.g. the demo gallery).
  maxCols?: number
  sid?: string | null
  t: Theme
}
