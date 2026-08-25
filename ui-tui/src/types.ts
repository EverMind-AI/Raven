// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import type { DagRunState } from './domain/dagRun.js'

export interface ActiveTool {
  context?: string
  id: string
  name: string
  startedAt?: number
}

export interface TodoItem {
  content: string
  id: string
  status: 'cancelled' | 'completed' | 'in_progress' | 'pending'
}

export interface ActivityItem {
  id: number
  text: string
  tone: 'error' | 'info' | 'warn'
}

/** Where a live delegated run's transcript can be read back from, while it runs. */
export type SubagentLiveRef = { callId?: string; kind: 'spawn' } | { kind: 'dag'; nodeId: string; runId: string }

export interface SubagentProgress {
  apiCalls?: number
  costUsd?: number
  depth: number
  durationSeconds?: number
  filesRead?: string[]
  filesWritten?: string[]
  goal: string
  id: string
  index: number
  inputTokens?: number
  iteration?: number
  liveRef?: SubagentLiveRef
  model?: string
  notes: string[]
  outputTail?: SubagentOutputEntry[]
  outputTokens?: number
  parentId: null | string
  reasoningTokens?: number
  startedAt?: number
  status: 'completed' | 'failed' | 'interrupted' | 'queued' | 'running'
  summary?: string
  taskCount: number
  thinking: string[]
  toolCount: number
  tools: string[]
  toolsets?: string[]
}

export interface SubagentOutputEntry {
  isError: boolean
  preview: string
  tool: string
}

export interface SubagentNode {
  aggregate: SubagentAggregate
  children: SubagentNode[]
  item: SubagentProgress
}

export interface SubagentAggregate {
  activeCount: number
  costUsd: number
  descendantCount: number
  filesTouched: number
  hotness: number
  inputTokens: number
  maxDepthFromHere: number
  outputTokens: number
  totalDuration: number
  totalTools: number
}

export interface DelegationStatus {
  active: {
    depth?: number
    goal?: string
    model?: null | string
    parent_id?: null | string
    started_at?: number
    status?: string
    subagent_id?: string
    tool_count?: number
  }[]
  max_concurrent_children?: number
  max_spawn_depth?: number
  paused: boolean
}

export interface ApprovalReq {
  approvalId: string
  command: string
  conversationId: string
  description: string
  // Absolute Unix deadline in milliseconds; the gateway wire value is seconds.
  expiresAt: number
}

export interface ConfirmReq {
  cancelLabel?: string
  confirmLabel?: string
  danger?: boolean
  defaultAnswer?: boolean
  detail?: string
  onConfirm?: () => void
  prompt?: string
  requestId?: string
  title?: string
}

export interface ClarifyReq {
  choices: string[] | null
  question: string
  requestId: string
}

// One tool invocation inside an episode. `summary` is a target-string ("ls
// biz/", "approve.go") or empty; the verb comes from the tool name at render.
export interface EpisodeTool {
  id: string
  name: string
  summary: string
  // The model's own one-line description of what a call is for, when the
  // transport sends one -- claude-agent-acp puts Claude's Bash `description`
  // in the call's arguments. Preferred over a derived label because it names
  // the intent rather than the programs; absent for every transport that sends
  // none, so a row without one is unaffected.
  intent?: string
  resultPreview?: string
  // A run_subagent_dag call's graph, pinned here when the run reported one. The
  // live store is cleared at turn end and the tool result is clamped to 200
  // chars, so this is what keeps the graph in the transcript.
  dag?: DagRunState
  diff?: string
  added?: number
  removed?: number
  ok: boolean
  done?: boolean
  // Wall-clock start, so an in-flight call can show a live elapsed timer
  // (durationMs is only known once it finishes).
  startedAt?: number
  durationMs?: number
}

// One model call. The transcript groups a turn into these; each collapses to a
// single summary line and expands to its reasoning + tools.
export interface Episode {
  index: number
  // When this model call began — lets the folded reasoning row show a live
  // duration while the step is still thinking.
  startedAt?: number
  reasoning?: string
  narration?: string
  tools: EpisodeTool[]
  durationMs?: number
  // Wall time spent before this step's first tool (≈ the model's thinking
  // time), so "reasoning for Ns" reflects thought, not tool-execution time.
  reasoningMs?: number
}

export interface Msg {
  info?: SessionInfo
  kind?: 'artifacts' | 'diff' | 'episodes' | 'intro' | 'panel' | 'slash' | 'trail'
  artifacts?: TurnArtifacts
  panelData?: PanelData
  role: Role
  text: string
  thinking?: string
  thinkingTokens?: number
  toolTokens?: number
  tools?: string[]
  episodes?: Episode[]
  /**
   * The identity of the turn this message was folded from, where it was folded
   * from one. Stable while that turn is still running and the runtime keeps
   * handing over a fresh object for it, which is what a fold has to be keyed on
   * to outlive the row under it.
   */
  foldId?: string
  todos?: TodoItem[]
  todoIncomplete?: boolean
  todoCollapsedByDefault?: boolean
}

export interface TurnArtifactFile {
  change?: 'edit' | 'new'
  ext: string
  missing?: boolean
  name: string
  size?: number
  title?: string
}

export interface TurnArtifacts {
  changes: TurnArtifactFile[]
  deliveries: TurnArtifactFile[]
}

export type Role = 'assistant' | 'system' | 'tool' | 'user'
export type DetailsMode = 'hidden' | 'collapsed' | 'expanded'
export type ThinkingMode = 'collapsed' | 'truncated' | 'full'

// Per-section overrides for the agent details accordion.  Resolution order
// at lookup time is: explicit `display.sections.<name>` → built-in
// SECTION_DEFAULTS → global `details_mode`.  Today the built-in defaults
// expand `thinking`/`tools` and hide `activity`; `subagents` falls through
// to the global mode.  Any explicit value still wins for that one section.
export type SectionName = 'thinking' | 'tools' | 'subagents' | 'activity'
export type SectionVisibility = Partial<Record<SectionName, DetailsMode>>

export interface McpServerStatus {
  connected: boolean
  name: string
  tools: number
  transport: string
}

export interface SessionInfo {
  context_window?: number
  cwd?: string
  // Which of a multi-endpoint provider's endpoints this session is on;
  // absent for single-endpoint providers, which have no label worth showing.
  endpoint?: string | null
  fast?: boolean
  lazy?: boolean
  mcp_servers?: McpServerStatus[]
  model: string
  model_id?: string
  provider?: string
  reasoning_effort?: string
  release_date?: string
  service_tier?: string
  skills: Record<string, string[]>
  system_prompt?: string
  // The resumed session's name. Absent on a fresh one, which has nothing to
  // name yet, so the panel simply has no line for it.
  title?: string | null
  tools: Record<string, string[]>
  update_available?: boolean | null
  update_command?: string
  usage?: Usage
  version?: string
}

export interface Usage {
  calls: number
  compressions?: number
  context_max?: number
  context_percent?: number
  context_used?: number
  cost_status?: string
  cost_usd?: number
  input: number
  output: number
  reasoning?: number
  total: number
}

export interface SudoReq {
  requestId: string
}

export interface SecretReq {
  envVar: string
  prompt: string
  requestId: string
}

export interface PanelData {
  sections: PanelSection[]
  title: string
}

export interface PanelSection {
  items?: string[]
  rows?: [string, string][]
  text?: string
  title?: string
}

export interface SlashCatalog {
  canon: Record<string, string>
  categories: SlashCategory[]
  pairs: [string, string][]
  skillCount: number
  sub: Record<string, string[]>
}

export interface SlashCategory {
  name: string
  pairs: [string, string][]
}
