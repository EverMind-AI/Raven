/* Builds one node's file-change preview from its own tool calls.
 *
 * `TaskFile` already carries the counts (`add` / `del` / `size`) `tasks.list`
 * computed server-side; this is only the patch BODY for a reader who opens
 * the chip, and the only place it can come from is the node's own tool
 * calls -- a sub-agent has no `deliver_files` and the wire never carries a
 * unified diff (contract §2.4 / §6 G1). A write is read as "all added",
 * the same convention `features/workspace/record.ts` already draws a plain
 * `write_file` as; an edit is the real before/after slice.
 */

import { fromEdit, fromWrite } from '../workspace/hunks'

import type { WsHunk } from '../workspace/types'
import type { NodeStep } from './types'

interface FileToolArgs {
  path?: string
  content?: string
  old_text?: string
  new_text?: string
}

function parseArgs(raw: string): FileToolArgs {
  try {
    const v: unknown = JSON.parse(raw)
    return v && typeof v === 'object' && !Array.isArray(v) ? (v as FileToolArgs) : {}
  } catch {
    return {}
  }
}

const isWrite = (name: string): boolean => /write/i.test(name)
const isEdit = (name: string): boolean => /edit/i.test(name)

/* The node's last tool call against `path`: a file rewritten twice in one
   node reads as its final shape, the same rule the session's own change list
   already applies (features/workspace/record.ts). */
export function hunkForFile(steps: readonly NodeStep[], path: string): WsHunk | null {
  const calls = steps.filter((s): s is Extract<NodeStep, { kind: 'tool' }> => s.kind === 'tool')
  for (let i = calls.length - 1; i >= 0; i -= 1) {
    const step = calls[i]!
    if (!isWrite(step.name) && !isEdit(step.name)) continue
    const args = parseArgs(step.args)
    if (args.path !== path) continue
    if (isEdit(step.name) && args.old_text !== undefined && args.new_text !== undefined) {
      return fromEdit(args.old_text, args.new_text)
    }
    if (args.content !== undefined) return fromWrite(args.content)
  }
  return null
}
