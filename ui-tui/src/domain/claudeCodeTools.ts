// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Claude Code's own tool names, and how a run of them folds.
//
// Same shape and same reason as `codexTools.ts`. A claude_code direct chat keeps
// Claude Code's names so the transcript reads as a Claude Code conversation, and
// `OVERRIDES` in `episodeSummary.ts` is keyed by Raven's vocabulary -- so these
// names need a table of their own. With no entry every call falls to the generic
// rule and a run of six reads stops collapsing to one line.
//
// The verb is the Claude Code name verbatim, never a Raven synonym. Only `style`
// and `unit` are ours.
//
// The entries are the names with evidence behind them: `Bash` and `Read` were
// measured on the wire, the rest are named explicitly in
// `@agentclientprotocol/claude-agent-acp`'s own `toolInfoFromToolUse` switch
// (`dist/tools.js:16`) or were already carried by `RAVEN_NAME`. A name outside
// this table is not a gap -- `ruleFor` humanises it, which is the right answer
// for a tool nobody here has seen.

import type { VerbRule } from './episodeSummary.js'

export const CLAUDE_VERBS: Record<string, VerbRule> = {
  Bash: { verb: 'Bash', unit: 'commands', style: 'target' },
  BashOutput: { verb: 'BashOutput', unit: 'commands', style: 'target' },
  Read: { verb: 'Read', unit: 'files', style: 'count' },
  Write: { verb: 'Write', unit: 'files', style: 'target' },
  Edit: { verb: 'Edit', unit: 'files', style: 'target' },
  NotebookEdit: { verb: 'NotebookEdit', unit: 'files', style: 'target' },
  Glob: { verb: 'Glob', unit: 'patterns', style: 'count' },
  Grep: { verb: 'Grep', unit: 'patterns', style: 'count' },
  LS: { verb: 'LS', unit: 'dirs', style: 'count' },
  WebFetch: { verb: 'WebFetch', unit: 'urls', style: 'target' },
  WebSearch: { verb: 'WebSearch', unit: 'queries', style: 'target' },
  Agent: { verb: 'Agent', unit: 'subagents', style: 'count' },
  Task: { verb: 'Task', unit: 'subagents', style: 'count' },
  TodoWrite: { verb: 'TodoWrite', unit: '', style: 'target' },
  ExitPlanMode: { verb: 'ExitPlanMode', unit: '', style: 'target' },
  AskUserQuestion: { verb: 'AskUserQuestion', unit: 'questions', style: 'target' },
  ReportFindings: { verb: 'ReportFindings', unit: 'findings', style: 'target' },
  TaskCreate: { verb: 'TaskCreate', unit: 'tasks', style: 'target' },
  TaskUpdate: { verb: 'TaskUpdate', unit: 'tasks', style: 'target' },
  TaskList: { verb: 'TaskList', unit: '', style: 'target' },
  TaskGet: { verb: 'TaskGet', unit: '', style: 'target' }
}

// An MCP call is named `mcp__<server>__<tool>`, one name per configured tool, so
// it cannot have a row of its own. The separator differs from codex's `mcp.`,
// which is why this is a second rule rather than a shared one.
export const claudeRule = (name: string): VerbRule | undefined =>
  CLAUDE_VERBS[name] ?? (name.startsWith('mcp__') ? { verb: name, unit: 'calls', style: 'target' } : undefined)
