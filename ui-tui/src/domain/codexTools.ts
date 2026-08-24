// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Codex's own tool names, and how a run of them folds.
//
// The runtime normalises most transports into Raven's vocabulary at the read
// boundary, and `OVERRIDES` in `episodeSummary.ts` is keyed by that vocabulary.
// Codex is the exception on purpose: its rows keep codex's own names so a direct
// chat reads as a codex conversation. That is why they need a table here -- with
// no entry, every codex call falls to the generic rule and a run of six reads
// stops collapsing to one line.
//
// The verb is the codex name verbatim, never a Raven synonym. Only `style` and
// `unit` are ours. `unit` is what `phraseFor` in episodeSummary.ts reads to
// decide how repeats collapse; `style` is declared on every row (`VerbRule`
// requires it) but not read anywhere in ui-tui/src yet -- `phraseFor` collapses
// a run by `tools.length` alone, so a row's `style` currently has no effect.

import type { VerbRule } from './episodeSummary.js'

export const CODEX_VERBS: Record<string, VerbRule> = {
  apply_patch: { verb: 'apply_patch', unit: 'files', style: 'target' },
  update_plan: { verb: 'update_plan', unit: '', style: 'target' },
  commandExecution: { verb: 'commandExecution', unit: 'commands', style: 'target' },
  'commandExecution.read': { verb: 'commandExecution.read', unit: 'files', style: 'count' },
  'commandExecution.listFiles': { verb: 'commandExecution.listFiles', unit: 'dirs', style: 'count' },
  'commandExecution.search': { verb: 'commandExecution.search', unit: 'patterns', style: 'count' },
  webSearch: { verb: 'webSearch', unit: 'queries', style: 'target' },
  fileChange: { verb: 'fileChange', unit: 'files', style: 'target' },
  imageView: { verb: 'imageView', unit: 'images', style: 'count' },
  imageGeneration: { verb: 'imageGeneration', unit: 'images', style: 'target' },
  mcpToolCall: { verb: 'mcpToolCall', unit: 'calls', style: 'target' },
  dynamicToolCall: { verb: 'dynamicToolCall', unit: 'calls', style: 'target' },
  collabAgentToolCall: { verb: 'collabAgentToolCall', unit: 'calls', style: 'target' },
  subAgentActivity: { verb: 'subAgentActivity', unit: 'subagents', style: 'target' },
  contextCompaction: { verb: 'contextCompaction', unit: '', style: 'target' }
}

// An MCP call is named `mcp.<server>.<tool>`, one name per configured tool, so
// it cannot have a row of its own.
export const codexRule = (name: string): VerbRule | undefined =>
  CODEX_VERBS[name] ?? (name.startsWith('mcp.') ? { verb: name, unit: 'calls', style: 'target' } : undefined)
