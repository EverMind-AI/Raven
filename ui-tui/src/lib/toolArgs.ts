// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

// Keys that name the "what" of a tool call. Tried first (in this order) when
// building a preview so the row shows the query/question/command rather than
// whatever argument happens to come first — e.g. web_search's numeric `count`,
// or ask_user's nested `questions` blob. Generic across tools: no per-tool
// table to keep in sync as tools are added.
const PREVIEW_KEYS = [
  'question',
  'query',
  'q',
  'command',
  'cmd',
  'pattern',
  'path',
  'file',
  'url',
  'prompt',
  'text',
  'goal',
  'task',
  'name'
]

// The first non-empty string reachable from a value, preferring PREVIEW_KEYS
// when descending into objects/arrays. Depth-bounded so a deeply nested (or
// cyclic-looking) argument blob can't run away. Non-string scalars (numbers,
// booleans) are skipped, so `count: 10` never becomes the preview.
const firstStringArg = (value: unknown, depth = 0): string | undefined => {
  if (typeof value === 'string') {
    return value.trim() || undefined
  }

  if (depth >= 4 || value == null || typeof value !== 'object') {
    return undefined
  }

  if (Array.isArray(value)) {
    for (const item of value) {
      const found = firstStringArg(item, depth + 1)

      if (found) {
        return found
      }
    }

    return undefined
  }

  const obj = value as Record<string, unknown>

  for (const key of PREVIEW_KEYS) {
    const found = firstStringArg(obj[key], depth + 1)

    if (found) {
      return found
    }
  }

  for (const val of Object.values(obj)) {
    const found = firstStringArg(val, depth + 1)

    if (found) {
      return found
    }
  }

  return undefined
}

// A short, human preview of a tool call's arguments: the query / question /
// command text, never a numeric flag or a raw JSON dump. Empty string when the
// call carries no string-ish argument (the row then shows just the verb).
export const argPreview = (args: Record<string, unknown>): string => firstStringArg(args) ?? ''

// Each DAG node's prompt template, keyed by node id, read off a
// `run_subagent_dag` call's own arguments. No progress event carries the template
// and no manifest inlines it, so the call args are the only source available
// before a node has run -- which is when the graph is first drawn.
export const dagPromptTemplates = (args: Record<string, unknown>): Record<string, string> => {
  const nodes = args['nodes']

  if (!Array.isArray(nodes)) {
    return {}
  }

  const out: Record<string, string> = {}

  for (const entry of nodes) {
    if (entry == null || typeof entry !== 'object') {
      continue
    }

    const node = entry as Record<string, unknown>
    const id = node['id']
    // The JSON Schema handed to the model is snake_case; the graph file humans
    // review in git is camelCase, and both reach this as authored.
    const template = node['prompt_template'] ?? node['promptTemplate']

    if (typeof id === 'string' && id && typeof template === 'string' && template.trim()) {
      out[id] = template
    }
  }

  return out
}
