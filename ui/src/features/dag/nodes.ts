/* The three ways a graph reaches the page, adapted to one shape.
 *
 * A `run_subagent_dag` card can learn its nodes from any of three sources, and
 * which one it gets is not a property of the surface drawing it:
 *
 * - the **call's own arguments**, when the model made the call itself. The whole
 *   request is there, inputs and prompt templates included, and it is there
 *   before the run starts;
 * - the **`dag.run_started` event**, when the engine dispatched the run rather
 *   than the model -- a `mode: dag` playbook load, whose arguments are
 *   `{name, params, fills}` and never carried a graph. Structure only: the event
 *   is a run announcement, not a copy of the request;
 * - **`dag.get`**, for a card restored from history whose events are long gone,
 *   and to fill in what the event does not carry.
 *
 * Each surface used to know its own subset of the fields, which is how the
 * transcript's node strip came to hold four of them: a field the adapter did not
 * name was a field the card could never show, whatever the source had. So the
 * adapters live here, next to the model they produce, and a surface asks for
 * nodes rather than reading a payload.
 *
 * `merge` is what makes the sources composable: structure arrives first and the
 * request follows, or the other way round, and neither may erase the other.
 */

import type { DagNode, NodeInput } from './types'

/* A dag call's `nodes` argument. Deliberately loose: this is a tool call the
   model wrote, so every field is a claim rather than a guarantee. */
interface ArgNode {
  id?: unknown
  subagent?: unknown
  instance?: unknown
  depends_on?: unknown
  prompt_template?: unknown
  inputs?: unknown
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '')
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [])

const inputsOf = (v: unknown): Record<string, NodeInput> | null => {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return null
  const out: Record<string, NodeInput> = {}
  Object.entries(v as Record<string, unknown>).forEach(([k, val]) => {
    if (typeof val === 'string') out[k] = val
    else if (val && typeof val === 'object' && !Array.isArray(val)) out[k] = val as NodeInput
  })
  return Object.keys(out).length ? out : null
}

/* Every adapter starts here, so all three produce the same key set. An absent
   key and an explicit null are the same thing to `??`, but not to a spread or to
   a caller counting fields -- and "the three sources yield one shape" is the
   whole claim this module makes. */
const blank = (id: string): DagNode => ({
  id,
  subagent: '',
  instance: null,
  depends_on: [],
  status: 'pending',
  started_at: null,
  ended_at: null,
  prompt_template: null,
  inputs: null,
})

/* The model's own call. Nodes with no id are dropped rather than given one: an
   id is how every other part of this domain names a node, and a placeholder
   would be a name nothing else in the run answers to. */
export function fromArgs(args: unknown): DagNode[] {
  const raw = (args as { nodes?: unknown } | null)?.nodes
  if (!Array.isArray(raw)) return []
  return raw
    .filter((n): n is ArgNode => !!n && typeof n === 'object' && typeof (n as ArgNode).id === 'string')
    .map((n) => ({
      ...blank(str(n.id)),
      subagent: str(n.subagent),
      instance: str(n.instance) || null,
      depends_on: strs(n.depends_on),
      prompt_template: str(n.prompt_template) || null,
      inputs: inputsOf(n.inputs),
    }))
}

/* `dag.run_started`. The graph arrives whole here, before any node runs, which
   is why a card built from it can lay itself out immediately and never has to
   move a box afterwards. */
export function fromStarted(payload: unknown): DagNode[] {
  const raw = (payload as { nodes?: unknown } | null)?.nodes
  if (!Array.isArray(raw)) return []
  return raw
    .filter((n): n is ArgNode => !!n && typeof n === 'object' && typeof (n as ArgNode).id === 'string')
    .map((n) => ({
      ...blank(str(n.id)),
      subagent: str(n.subagent),
      instance: str(n.instance) || null,
      depends_on: strs(n.depends_on),
    }))
}

/* One row of `dag.get`'s `run.files`. Named `node` rather than `id` on the wire,
   which is the one shape difference between this source and the other two. */
export interface SnapshotRow {
  node?: unknown
  subagent?: unknown
  instance?: unknown
  depends_on?: unknown
  status?: unknown
  started_at?: unknown
  ended_at?: unknown
  prompt_template?: unknown
  inputs?: unknown
}

const num = (v: unknown): number | null => (typeof v === 'number' && isFinite(v) ? v : null)

/* `dag.get`. The only source that carries state as well as structure, because it
   is the only one read after the fact. */
export function fromSnapshot(files: unknown): DagNode[] {
  if (!Array.isArray(files)) return []
  return files
    .filter((f): f is SnapshotRow => !!f && typeof f === 'object' && typeof (f as SnapshotRow).node === 'string')
    .map((f) => ({
      ...blank(str(f.node)),
      subagent: str(f.subagent),
      instance: str(f.instance) || null,
      depends_on: strs(f.depends_on),
      status: str(f.status) || 'pending',
      started_at: num(f.started_at),
      ended_at: num(f.ended_at),
      prompt_template: str(f.prompt_template) || null,
      inputs: inputsOf(f.inputs),
    }))
}

/* Later facts on top of earlier ones, per node, without either source erasing
   what it does not know.
 *
 * The asymmetry is the point. A field the incoming source did not supply keeps
 * the value it had -- `dag.run_started` carries no prompt template, and taking
 * its silence for "no template" is how a card that had the whole request showed
 * an empty one. Order is the existing list's; nodes only in the incoming one are
 * appended, which is the case where the card had nothing to begin with. */
export function merge(have: DagNode[], incoming: DagNode[]): DagNode[] {
  if (!have.length) return incoming
  const by = new Map(incoming.map((n) => [n.id, n]))
  const out = have.map((n) => {
    const next = by.get(n.id)
    if (!next) return n
    by.delete(n.id)
    return {
      ...n,
      subagent: next.subagent || n.subagent,
      instance: next.instance ?? n.instance,
      depends_on: next.depends_on.length ? next.depends_on : n.depends_on,
      status: next.status || n.status,
      started_at: next.started_at ?? n.started_at,
      ended_at: next.ended_at ?? n.ended_at,
      prompt_template: next.prompt_template ?? n.prompt_template,
      inputs: next.inputs ?? n.inputs,
    }
  })
  return [...out, ...by.values()]
}

/* One node's status, times and nothing else: what `dag.node_updated` reports.
   Update-only on purpose -- an event for a node the card does not hold is an
   event for another run, and inventing a box for it would draw a graph the
   server never described. */
export function applyUpdate(
  nodes: DagNode[],
  update: { node?: unknown; status?: unknown; started_at?: unknown; ended_at?: unknown },
): DagNode[] {
  const id = str(update.node)
  if (!id || !nodes.some((n) => n.id === id)) return nodes
  return nodes.map((n) =>
    n.id === id
      ? {
          ...n,
          status: str(update.status) || n.status,
          started_at: num(update.started_at) ?? n.started_at,
          ended_at: num(update.ended_at) ?? n.ended_at,
        }
      : n,
  )
}
