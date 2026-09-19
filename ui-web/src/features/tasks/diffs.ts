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

import { fromEdit, fromWrite } from '../../lib/hunks'

import type { WsHunk } from '../../lib/hunks'
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

/* Every tool call the node made against `path`, in the order it made them.
   A `TaskFile` folds a node's touches of one path into one item whose counts
   are the sum of every touch, so the patch a reader opens from it shows every
   touch too: a write is the whole content as added, an edit the before/after
   slice. Summing these hunks' counts gives the chip's own numbers back. */
export function hunksForFile(steps: readonly NodeStep[], path: string): WsHunk[] {
  const out: WsHunk[] = []
  steps.forEach((step) => {
    if (step.kind !== 'tool' || (!isWrite(step.name) && !isEdit(step.name))) return
    const args = parseArgs(step.args)
    if (args.path !== path) return
    if (isEdit(step.name) && args.old_text !== undefined && args.new_text !== undefined) {
      out.push(fromEdit(args.old_text, args.new_text))
    } else if (args.content !== undefined) {
      out.push(fromWrite(args.content))
    }
  })
  return out
}
