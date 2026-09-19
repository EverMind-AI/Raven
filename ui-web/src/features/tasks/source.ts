/* -- tasks: the source --------------------------------------------------
   Everything this domain knows about speaking to the gateway, plus the pure
   functions that turn a `dag.node` / `subagent.context` answer into the one
   `NodeRecord` shape the node panel reads.

   `tasks.list` is the one read: a session's rows, or one row addressed by
   (kind, id). Stopping a run is one call dispatched
   by `row.kind` rather than two verbs the renderer has to choose between; a
   node's own record is read on demand, never carried on the row.
*/

import { current as sessionCurrent } from '../../lib/session'
import { gateway } from '../../rpc/gateway'

import type {
  DagNodeDetail, SubagentContextResult, TranscriptMessage,
} from '../../rpc/generated'
import type { NodeRecord, NodeStep, TaskKind, TaskNode, TaskRow, TasksSource } from './types'

const openKey = (): string => sessionCurrent() || ''

/* Every call here is addressed to the conversation on screen; a node's record
   is fetched fresh each time it is opened, per the contract's own reasoning --
   a running node's record is not something a snapshot can hold. */

export const tasksSource: TasksSource = {
  list: (sessionKey: string) =>
    gateway().call('tasks.list', { session_key: sessionKey }).then((r) => r.tasks || []),
  one: (kind: TaskKind, id: string) =>
    gateway().call('tasks.list', { session_key: openKey(), kind, id })
      .then((r) => (r.tasks && r.tasks[0]) || null),
  /* A dag is stopped by its run id, whichever process started it; a spawn by
     the handle it committed under (its instance, or its task id when nobody
     named one -- `TaskRow.handle` already resolved that). */
  stop: (row: TaskRow) => {
    if (row.kind === 'dag') {
      return gateway().call('subagent.interrupt', { subagent_id: row.id }).then((r) => !!r.found)
    }
    return gateway()
      .call('subagent.cancel_instance', { session_key: openKey(), agent: row.agent || '', handle: row.handle || '' })
      .then((r) => !!r.found)
  },
  node: (row: TaskRow, node: TaskNode) => {
    if (row.kind === 'dag') {
      return gateway().call('dag.node', { run_id: row.id, node: node.node_id, session_key: openKey() })
        .then((r) => fromDagNode(r.node))
    }
    return gateway().call('subagent.context', { id: node.node_id, session_id: openKey() })
      .then((r) => fromSpawnContext(r))
  },
  roster: () => gateway().call('subagents.list', { probe: false }).then((r) => r.rows || []),
}

/* -- mapping a wire answer into one node's record ----------------------- */

/* The first entry is the dispatch (the rendered prompt, upstream output
   already fenced) when the transport wrote it as a plain `user` turn -- every
   lane's `session.resume` shape does. */
function splitDispatch(messages: TranscriptMessage[]): { dispatch: TranscriptMessage | null; rest: TranscriptMessage[] } {
  const first = messages[0]
  if (first && first.role === 'user') return { dispatch: first, rest: messages.slice(1) }
  return { dispatch: null, rest: messages }
}

/* The trailing entry is the answer when it is a plain assistant turn -- one
   with no tool call still open. A run that stopped mid-tool, or failed, or is
   still going, ends in something else, and there is no answer to pull out. */
function splitAnswer(messages: TranscriptMessage[]): { answer: TranscriptMessage | null; body: TranscriptMessage[] } {
  const last = messages[messages.length - 1]
  if (last && last.role === 'assistant' && !(last.tool_calls && last.tool_calls.length)) {
    return { answer: last, body: messages.slice(0, -1) }
  }
  return { answer: null, body: messages }
}

/* Every step between the dispatch and the answer, in the order they
   happened: a thought (an assistant entry's `reasoning_content`), something
   said between tool calls (the same entry's own `text`, when the entry is
   not the trailing answer -- `fromMessages` has already carved that off), a
   tool call (matched to the later `role: 'tool'` entry that answers it by
   `tool_call_id`), or a `role: 'console'` entry -- the live cli lane's
   synthetic stand-in for a channel that keeps no transcript of its own
   A `tool` entry with nothing pointing at it is a
   result this page never asked to see rendered on its own row, so it is
   consumed silently rather than drawn a second time. */
export function stepsOf(messages: TranscriptMessage[]): NodeStep[] {
  const results = new Map<string, TranscriptMessage>()
  messages.forEach((m) => { if (m.role === 'tool' && m.tool_call_id) results.set(m.tool_call_id, m) })
  const steps: NodeStep[] = []
  messages.forEach((m) => {
    if (m.role === 'tool') return
    if (m.role === 'console') { steps.push({ kind: 'console', text: m.text || '' }); return }
    if (m.role !== 'assistant') return
    if (m.reasoning_content) steps.push({ kind: 'think', text: m.reasoning_content })
    if (m.text) steps.push({ kind: 'say', text: m.text })
    for (const call of m.tool_calls || []) {
      const res = call.id ? results.get(call.id) : undefined
      steps.push({
        kind: 'tool',
        id: call.id,
        name: call.name,
        args: call.arguments,
        result: res ? (res.text ?? '') : null,
        /* Anchored at the start for the plain error words, but not for the
           infra codes: a call whose output starts clean and ends in a
           traceback, or opens with an HTTP 429 body, is still a failure the
           reader should see marked, wherever in the text it shows up. */
        ok: res ? !/^\s*(error|failed?)\b|\b(429|ENOENT|Traceback)\b/i.test(res.text || '') : null,
      })
    }
  })
  return steps
}

function fromMessages(messages: TranscriptMessage[] | undefined, promptFallback: string | null): Omit<NodeRecord, 'outputTruncated'> {
  const all = messages || []
  const { dispatch, rest } = splitDispatch(all)
  const { answer, body } = splitAnswer(rest)
  return {
    dispatch: dispatch ? (dispatch.text ?? null) : promptFallback,
    steps: stepsOf(body),
    answer: answer ? (answer.text ?? null) : null,
  }
}

/* `dag.node`. The rendered prompt is a field of its own on the answer, ahead
   of `messages` -- preferred over the messages' own first entry so a node
   whose record has not been read (an empty `messages`) still shows what it
   was asked. */
export function fromDagNode(detail: DagNodeDetail): NodeRecord {
  const mapped = fromMessages(detail.messages, detail.prompt ?? null)
  return {
    ...mapped,
    dispatch: detail.prompt ?? mapped.dispatch,
    outputTruncated: !!detail.output_truncated,
  }
}

/* `subagent.context`. No `prompt` field of its own -- the first message is
   the whole of it -- and no `output_truncated`: the field is a `dag.node`
   fact about reading `.out.md`'s head, and spawn's answer is the last message
   whole. */
export function fromSpawnContext(ctx: SubagentContextResult): NodeRecord {
  return { ...fromMessages(ctx.messages, null), outputTruncated: false }
}
