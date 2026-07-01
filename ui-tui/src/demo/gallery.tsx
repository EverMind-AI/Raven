// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
//
// Component gallery — a standalone harness for previewing/restyling TUI
// components in isolation, without wiring demo args into the main program.
// Run via `npm run demo` (see package.json).
//
// Layout: a left sidebar lists pages; the main area renders the selected
// page's demos. Only the active page is mounted, so components on other pages
// don't compete for input or screen space. Switch pages with Tab / ←/→;
// within a page the focused prompt still owns its own keys (arrows/enter).
//
// To add components: append to an existing *Page, or add a new page to PAGES.

import type { ReactNode } from 'react'

import { Box, render, Text, useApp, useInput, useStdout } from '@hermes/ink'
import { useState } from 'react'

import type { ApprovalReq, ClarifyReq, ConfirmReq, PanelSection, SessionInfo, Usage } from '../types.js'

import { FloatBox, StatusRule } from '../components/appChrome.js'
import { Banner, Panel, SessionPanel } from '../components/branding.js'
import { ApprovalPrompt, ClarifyPrompt, ConfirmPrompt } from '../components/prompts.js'
import { DEFAULT_THEME } from '../theme.js'

const t = DEFAULT_THEME
const noop = () => {}
const SIDEBAR_W = 16

if (!process.stdin.isTTY) {
  process.stderr.write('component-gallery: run in an interactive terminal (needs a TTY)\n')
  process.exit(0)
}

// ── Mock data ────────────────────────────────────────────────────────

const approvalReq: ApprovalReq = {
  command: 'rm -rf dist\nnpm ci\nnpm run build',
  description: 'run a shell command'
}

const clarifyChoicesReq: ClarifyReq = {
  choices: ['pnpm', 'npm', 'yarn'],
  question: 'Which package manager should I use?',
  requestId: 'demo-clarify-choices'
}

const clarifyFreeReq: ClarifyReq = {
  choices: null,
  question: 'Describe what went wrong:',
  requestId: 'demo-clarify-free'
}

const confirmReq: ConfirmReq = {
  cancelLabel: 'No',
  confirmLabel: 'Yes',
  detail: 'Edits src/theme.ts and 2 more files',
  title: 'Apply 3 changes?'
}

const confirmDangerReq: ConfirmReq = {
  cancelLabel: 'Cancel',
  confirmLabel: 'Delete',
  danger: true,
  detail: 'This cannot be undone',
  title: 'Delete workspace?'
}

const usage: Usage = {
  calls: 12,
  compressions: 2,
  context_max: 200000,
  context_percent: 42,
  context_used: 84210,
  cost_usd: 0.1234,
  input: 60000,
  output: 24210,
  total: 84210
}

const sessionInfo: SessionInfo = {
  cwd: '~/raven',
  mcp_servers: [
    { connected: true, name: 'chrome-devtools', tools: 12, transport: 'stdio' },
    { connected: false, name: 'figma', tools: 0, transport: 'sse' }
  ],
  model: 'anthropic/claude-opus-4-8',
  model_id: 'anthropic/claude-opus-4-8',
  provider: 'anthropic',
  release_date: '2026-06-18',
  skills: {
    cli: ['init', 'review', 'security-review'],
    figma: ['figma-use', 'figma-generate-design'],
    research: ['deep-research']
  },
  system_prompt: 'You are Raven, a coding agent…',
  tools: {
    core_tools: ['read', 'write', 'edit', 'bash'],
    search_tools: ['grep', 'glob']
  },
  update_behind: 2,
  update_command: 'raven update',
  version: '0.0.1'
}

const panelSections: PanelSection[] = [
  {
    rows: [
      ['Model', 'claude-opus-4-8'],
      ['Provider', 'Anthropic']
    ],
    title: 'Overview'
  },
  { items: ['first item', 'second item', 'third item'] },
  { text: 'A free-form text section, e.g. a help blurb.' }
]

// ── Section wrapper ──────────────────────────────────────────────────

function Demo({ children, title }: { children: ReactNode; title: string }) {
  return (
    <Box flexDirection="column" marginTop={1}>
      <Text color={t.color.label}>{`▸ ${title}`}</Text>
      <Box marginTop={1}>{children}</Box>
    </Box>
  )
}

// ── Pages ────────────────────────────────────────────────────────────

function PromptsPage() {
  return (
    <Box flexDirection="column">
      <Demo title="ApprovalPrompt">
        <ApprovalPrompt onChoice={noop} req={approvalReq} t={t} />
      </Demo>
      <Demo title="ClarifyPrompt — choices">
        <ClarifyPrompt onAnswer={noop} onCancel={noop} req={clarifyChoicesReq} t={t} />
      </Demo>
      <Demo title="ClarifyPrompt — free text">
        <ClarifyPrompt onAnswer={noop} onCancel={noop} req={clarifyFreeReq} t={t} />
      </Demo>
      <Demo title="ConfirmPrompt">
        <ConfirmPrompt onCancel={noop} onConfirm={noop} req={confirmReq} t={t} />
      </Demo>
      <Demo title="ConfirmPrompt — danger">
        <ConfirmPrompt onCancel={noop} onConfirm={noop} req={confirmDangerReq} t={t} />
      </Demo>
    </Box>
  )
}

function AppChromePage() {
  // Width the real status bar would get: terminal minus the sidebar.
  const cols = Math.max(40, (useStdout().stdout?.columns ?? 100) - SIDEBAR_W - 4)
  const now = Date.now()

  return (
    <Box flexDirection="column">
      <Demo title="StatusRule — idle">
        <StatusRule
          bgCount={1}
          busy={false}
          cols={cols}
          cwdLabel="~/raven"
          model="anthropic/claude-opus-4-8"
          modelReasoningEffort="high"
          sessionStartedAt={now - 125_000}
          showCost
          status="ready"
          statusColor={t.color.statusGood}
          t={t}
          turnStartedAt={null}
          usage={usage}
        />
      </Demo>
      <Demo title="StatusRule — busy (animated)">
        <StatusRule
          bgCount={0}
          busy
          cols={cols}
          cwdLabel="~/raven"
          model="anthropic/claude-opus-4-8"
          sessionStartedAt={now - 125_000}
          showCost={false}
          status="working"
          statusColor={t.color.statusWarn}
          t={t}
          turnStartedAt={now - 5_000}
          usage={usage}
        />
      </Demo>
      <Demo title="FloatBox">
        <FloatBox color={t.color.border}>
          <Text color={t.color.text}>Floating panel content</Text>
          <Text color={t.color.muted}>rendered inside a rounded FloatBox</Text>
        </FloatBox>
      </Demo>

      <Box marginTop={1}>
        <Text color={t.color.muted}>
          (GoodVibesHeart, TranscriptScrollbar, StickyPromptTracker need live scroll/animation state — omitted)
        </Text>
      </Box>
    </Box>
  )
}

function BrandingPage() {
  // Width available to the page content: terminal minus the sidebar + margins.
  const cols = Math.max(40, (useStdout().stdout?.columns ?? 100) - SIDEBAR_W - 4)

  return (
    <Box flexDirection="column">
      <Demo title="Banner (logo)">
        <Banner t={t} />
      </Demo>
      <Demo title="SessionPanel">
        <SessionPanel info={sessionInfo} maxCols={cols} sid="a1b2c3d4" t={t} />
      </Demo>
      <Demo title="Panel">
        <Panel sections={panelSections} t={t} title="Example Panel" />
      </Demo>

      <Box marginTop={1}>
        <Text color={t.color.muted}>
          (SessionPanel/Banner are full-width — widen the terminal if they wrap; sections toggle on click)
        </Text>
      </Box>
    </Box>
  )
}

const PAGES: { Page: () => ReactNode; title: string }[] = [
  { Page: BrandingPage, title: 'Branding' },
  { Page: AppChromePage, title: 'App Chrome' },
  { Page: PromptsPage, title: 'Prompts' }
]

// ── Shell ────────────────────────────────────────────────────────────

function Gallery() {
  const [page, setPage] = useState(0)
  const { exit } = useApp()

  useInput((ch, key) => {
    if (ch === 'q') {
      exit()
    } else if (key.rightArrow || (key.tab && !key.shift)) {
      setPage(p => (p + 1) % PAGES.length)
    } else if (key.leftArrow || (key.tab && key.shift)) {
      setPage(p => (p - 1 + PAGES.length) % PAGES.length)
    }
  })

  const Active = PAGES[page]!.Page

  return (
    <Box paddingX={1} paddingY={1}>
      <Box flexDirection="column" marginRight={2} width={SIDEBAR_W}>
        <Text bold color={t.color.primary}>
          Gallery
        </Text>
        <Text />
        {PAGES.map((p, i) => (
          <Text bold={i === page} color={i === page ? t.color.accent : t.color.muted} key={p.title}>
            {i === page ? '▸ ' : '  '}
            {p.title}
          </Text>
        ))}
        <Text />
        <Text color={t.color.muted}>Tab/←/→ page</Text>
        <Text color={t.color.muted}>Ctrl+C/q quit</Text>
      </Box>

      <Box flexDirection="column" flexGrow={1}>
        <Active />
      </Box>
    </Box>
  )
}

render(<Gallery />)
