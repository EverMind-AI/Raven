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

import { applyEdit, fromDelete, fromEdit, fromWrite } from '../../lib/hunks'

import type { WsHunk } from '../../lib/hunks'
import type { NodeStep, TaskFile } from './types'

interface FileToolArgs {
  path?: string
  content?: string
  old_text?: string
  new_text?: string
  replace_all?: boolean
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

/* The record stores a path relative to the run's workspace while the tool
   call may have named it absolutely (or the other way round for records
   written before the runtime relativised them), so the two match when one
   ends with the other at a segment boundary. */
function samePath(a: string | undefined, b: string): boolean {
  if (a === undefined) return false
  if (a === b) return true
  const [long, short] = a.length >= b.length ? [a, b] : [b, a]
  return long.endsWith(`/${short}`)
}

/* Every tool call the node made against `path`, in the order it made them.
   A `TaskFile` folds a node's touches of one path into one item whose counts
   are the sum of every touch, so the patch a reader opens from it shows every
   touch too: a write is the whole content as added, an edit the before/after
   slice. Summing these hunks' counts gives the chip's own numbers back.

   A file the node then removed ends on one more hunk, every line of it taken
   back out. The contents can only be what the node's own calls left there --
   the wire carries no deleted file's body for a task, and the removal itself
   reaches the lane as a count -- so the body is followed call by call: a write
   is the whole file, an edit is applied to it the way the tool applies it
   (`applyEdit`). An edit that cannot be followed ends the following, and the
   deletion then has no body to show; the chip's own counts still say what
   went. */
export function hunksForFile(
  steps: readonly NodeStep[], path: string, op?: TaskFile['op'],
): WsHunk[] {
  const out: WsHunk[] = []
  let body: string | null = null
  steps.forEach((step) => {
    if (step.kind !== 'tool' || (!isWrite(step.name) && !isEdit(step.name))) return
    const args = parseArgs(step.args)
    if (!samePath(args.path, path)) return
    if (isEdit(step.name) && args.old_text !== undefined && args.new_text !== undefined) {
      out.push(fromEdit(args.old_text, args.new_text))
      body = body == null ? null : applyEdit(body, args.old_text, args.new_text, args.replace_all === true)
    } else if (args.content !== undefined) {
      body = args.content
      out.push(fromWrite(args.content))
    }
  })
  if (op === 'delete' && body != null) out.push(fromDelete(body))
  return out
}
