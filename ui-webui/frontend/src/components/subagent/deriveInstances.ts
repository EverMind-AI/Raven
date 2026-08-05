import type { ContentBlock, Msg } from '@agentscope-ai/agentscope/message';

import type { RavenSubagentInstance, RavenThirdPartySubagent } from '@/api';
import type { DagRunLive } from '@/components/chat/DagRunsContext';
import { parseInput } from '@/components/chat/tool-renderers/_shared';
import { buildRunIdByToolCallId, RUN_SUBAGENT_DAG_TOOL } from '@/components/dag/deriveDag';

/** Where a DAG-node exchange's prompt and output can be read back from. */
export interface ExchangeSource {
	runId: string;
	node: string;
}

export interface Exchange {
	prompt: string;
	output: string;
	action: 'create' | 'resume' | 'unknown';
	/** Set for a DAG-node invocation. The rendered prompt and the output text
	 *  live in the run dir, not the transcript, so the row fetches them on
	 *  demand from `raven.subagents.dag.node` when it is expanded. */
	source?: ExchangeSource;
}

export interface Instance {
	/** Row identity. A handle alone is not unique: the same handle can be
	 *  reused under two agents, and a bare DAG node id repeats across runs. */
	key: string;
	/** What the row is labelled with — the instance handle, or the node id. */
	handle: string;
	prototype: string;
	agentId?: string;
	exchanges: Exchange[];
	/** Whether the instance is resumable. Decides whether a row with nothing in
	 *  flight reads as `idle` (alive, awaiting the next turn) or as its last
	 *  invocation's terminal status. */
	stateful: boolean;
	/** The DAG run this instance's invocations belong to. Present only for
	 *  DAG-driven instances; stopping such a row cancels the whole run, since
	 *  node-level cancellation is not offered. */
	runId?: string;
}

/** The `nodes` array of one `run_subagent_dag` call, as submitted. */
interface CallNode {
	id?: string;
	subagent?: string;
	instance?: string | null;
	prompt_template?: string;
}

/**
 * Derive the session's sub-agent instances from the transcript.
 *
 * The panel lists *sub-agent instances*, never DAG runs — the DAG itself is
 * drawn in the conversation, and duplicating it here as a run row said nothing
 * the graph did not already say. Every sub-agent invocation reaches the list
 * through one of two passes, both keyed by row identity:
 *
 * 1. ``spawn`` calls — every raven sub-agent goes through the one ``spawn``
 *    tool, so this pass matches ``call.name === 'spawn'``, reads the prototype
 *    off ``input.agent`` and the handle off ``input.instance``. ``spawn`` is
 *    fire-and-forget: its tool result is an ack string ("Subagent [...]
 *    started..."), not the sub-agent's reply, which only arrives later as a
 *    separate announce turn with no handle to correlate it back by. So this
 *    pass recovers each exchange's **prompt** only; its output stays empty
 *    rather than showing the ack as if it were a reply.
 * 2. ``run_subagent_dag`` nodes — each node is a sub-agent invocation, so each
 *    contributes an exchange. Nodes sharing an ``instance`` handle fold into
 *    one multi-turn row (that is what the handle means: the same conversation,
 *    resumed); a node without one is its own single-turn row, keyed by run and
 *    node id. Structure comes from the **call arguments**, not the manifest or
 *    the live overlay: the args carry the ``prompt_template`` neither of the
 *    other two does, and they are in the transcript, so they are the only
 *    source that is there before the run starts and still there after a reload.
 *    A node's output is not in the transcript at all — the exchange records
 *    where to fetch it from instead (see ``Exchange.source``).
 *
 * @param statefulNames - Prototypes configured with a resume command, i.e. the
 *   ones an instance handle can actually resume.
 * @param liveDagRuns - Live per-run DAG overlay keyed by ``run_id`` (from
 *   ``DagRunsContext``), used only to bind each DAG call to its run id.
 */
export function deriveInstances(
	msgs: Msg[],
	statefulNames: Set<string>,
	liveDagRuns: Record<string, DagRunLive> = {},
): Instance[] {
	const calls: { name: string; input: string; id: string }[] = [];
	const results = new Map<string, { metadata?: Record<string, unknown> }>();
	for (const msg of msgs) {
		const blocks: ContentBlock[] = Array.isArray(msg.content) ? msg.content : [];
		for (const block of blocks) {
			if (block.type === 'tool_call') {
				calls.push({ name: block.name, input: block.input, id: block.id });
			} else if (block.type === 'tool_result') {
				results.set(block.id, block);
			}
		}
	}

	const byKey = new Map<string, Instance>();
	const record = (seed: Omit<Instance, 'exchanges'>, exchange: Omit<Exchange, 'action'>) => {
		let inst = byKey.get(seed.key);
		if (!inst) {
			inst = { ...seed, exchanges: [] };
			byKey.set(seed.key, inst);
		}
		if (!inst.agentId && seed.agentId) inst.agentId = seed.agentId;
		inst.exchanges.push({
			...exchange,
			// No metadata distinguishes a create from a resume, so position in
			// the row's own history is the only signal: the first invocation
			// started the conversation, later ones continued it.
			action: inst.exchanges.length === 0 ? 'create' : 'resume',
		});
	};

	for (const call of calls) {
		if (call.name !== 'spawn') continue;
		const input = parseInput(call.input);
		const prototype = typeof input.agent === 'string' ? input.agent : undefined;
		if (!prototype || !statefulNames.has(prototype)) continue;
		const handle = typeof input.instance === 'string' ? input.instance : undefined;
		if (!handle) continue;
		const meta = (results.get(call.id)?.metadata ?? {}) as Record<string, unknown>;
		record(
			{
				key: `${prototype}/${handle}`,
				handle,
				prototype,
				agentId: typeof meta.agent_id === 'string' ? meta.agent_id : undefined,
				stateful: true,
			},
			{ prompt: typeof input.task === 'string' ? input.task : '', output: '' },
		);
	}

	const runIdByToolCallId = buildRunIdByToolCallId(msgs, liveDagRuns);
	for (const call of calls) {
		if (call.name !== RUN_SUBAGENT_DAG_TOOL) continue;
		const runId = runIdByToolCallId[call.id];
		const parsed = parseInput(call.input) as { nodes?: CallNode[] };
		const nodes = Array.isArray(parsed.nodes) ? parsed.nodes : [];
		for (const n of nodes) {
			if (typeof n.id !== 'string' || typeof n.subagent !== 'string') continue;
			const handle = typeof n.instance === 'string' && n.instance ? n.instance : null;
			record(
				{
					// A stateful node folds into its handle's row across runs; a
					// one-shot node is scoped to its run so the same node id in a
					// later run is a separate row.
					key: handle
						? `${n.subagent}/${handle}`
						: `${n.subagent}/${runId ?? call.id}/${n.id}`,
					handle: handle ?? n.id,
					prototype: n.subagent,
					stateful: handle !== null && statefulNames.has(n.subagent),
					runId,
				},
				{
					prompt: typeof n.prompt_template === 'string' ? n.prompt_template : '',
					output: '',
					source: runId ? { runId, node: n.id } : undefined,
				},
			);
		}
	}

	return [...byKey.values()];
}

/**
 * Derive a prototype's transport from its Raven config, used as a fallback
 * when no live overlay entry is available yet.
 */
export function transportOf(prototype: string, subagents: RavenThirdPartySubagent[]): string {
	const cfg = subagents.find((s) => s.name === prototype);
	if (!cfg) return 'cli';
	if (cfg.kind === 'openai') return 'openai';
	if (cfg.transcriptFormat === 'codex_jsonl') return 'codex';
	if (cfg.transcriptFormat === 'claude_stream_json') return 'claude';
	return 'cli';
}

/** One row as rendered: the transcript's instance plus the registry's view of
 *  it. A row the transcript could not account for carries no `exchanges`; a row
 *  the registry has never written carries no `status`. */
export type MonitorInstance = Instance & {
	agent?: string;
	status?: InstanceRowStatus;
};

/** A DAG node registry row's identity, matching `Exchange.source`. */
function dagNodeKey(runId: string, node: string): string {
	return `${runId}/${node}`;
}

/**
 * Merge the transcript-derived instances with the registry's rows.
 *
 * The registry is authoritative for status and survives a reload; the
 * transcript supplies the exchange history. Either side can hold a row the
 * other does not, so both are walked and neither is dropped: an unmatched
 * registry row becomes a row with no exchanges, and a node the run never
 * reached stays a row with no status.
 *
 * The subtlety is that one invocation can own *two* registry rows. A DAG node
 * always has a `dag-node` row (its status), and when its sub-agent is a
 * resumable CLI it also has a `cli` row from `commit` recording that CLI's own
 * session id — keyed by the node id, because a node that declares no instance
 * handle commits under its task_id, which is the node id. Both must be claimed
 * by the same row or the node renders twice, the second time as a statusless
 * duplicate.
 *
 * Row order is transcript order, then leftovers. Deliberately not sorted by
 * recency: rows that reorder themselves mid-run are hard to follow.
 */
export function mergeRegistryRows(
	derived: Instance[],
	registry: RavenSubagentInstance[],
): MonitorInstance[] {
	const cliByKey = new Map<string, RavenSubagentInstance>();
	const dagByNode = new Map<string, RavenSubagentInstance>();
	for (const r of registry) {
		if (r.kind === 'dag-node') {
			if (r.runId && r.nodeId) dagByNode.set(dagNodeKey(r.runId, r.nodeId), r);
		} else {
			cliByKey.set(`${r.agent}/${r.handle}`, r);
		}
	}

	const rows: MonitorInstance[] = [];
	const claimedCli = new Set<string>();
	const claimedDag = new Set<string>();

	for (const inst of derived) {
		const statuses: string[] = [];
		const cliKeys = [inst.key];
		for (const ex of inst.exchanges) {
			if (!ex.source) continue;
			const key = dagNodeKey(ex.source.runId, ex.source.node);
			claimedDag.add(key);
			const row = dagByNode.get(key);
			if (row?.status) statuses.push(row.status);
			// The `commit` row for this node, if its sub-agent is a resumable CLI.
			cliKeys.push(`${inst.prototype}/${ex.source.node}`);
		}
		let cli: RavenSubagentInstance | undefined;
		for (const key of cliKeys) {
			const row = cliByKey.get(key);
			if (!row) continue;
			claimedCli.add(key);
			cli = cli ?? row;
			if (row.status) statuses.push(row.status);
		}
		rows.push({
			...inst,
			agent: cli?.agent ?? inst.prototype,
			agentId: inst.agentId ?? cli?.agentId,
			status: rowStatusOf(statuses, inst.stateful),
		});
	}

	for (const [key, r] of cliByKey) {
		if (claimedCli.has(key)) continue;
		rows.push({
			key,
			handle: r.handle,
			prototype: r.agent,
			agent: r.agent,
			agentId: r.agentId,
			exchanges: [],
			stateful: false,
			status: r.status,
		});
	}
	for (const [key, r] of dagByNode) {
		if (claimedDag.has(key)) continue;
		rows.push({
			key: `${r.agent}/${key}`,
			handle: r.nodeId ?? r.handle,
			prototype: r.agent,
			agent: r.agent,
			exchanges: [],
			stateful: false,
			runId: r.runId,
			status: r.status,
		});
	}
	return rows;
}

/** Row status vocabulary: the registry's, plus `idle` for a resumable instance
 *  with nothing currently in flight. */
export type InstanceRowStatus =
	| 'idle'
	| 'pending'
	| 'running'
	| 'completed'
	| 'failed'
	| 'skipped'
	| 'cancelled'
	| 'interrupted';

/**
 * Collapse an instance's per-invocation statuses into the row's status.
 *
 * In-flight wins outright — a resumable instance whose second turn is running
 * is "running", not "idle". With nothing in flight, a stateful instance is
 * `idle` (the process is gone but the conversation can be resumed, which is
 * what the handle is for) unless its last turn actually went wrong, which is
 * worth keeping on screen. A one-shot invocation just reports how it ended.
 */
export function rowStatusOf(statuses: string[], stateful: boolean): InstanceRowStatus | undefined {
	if (statuses.includes('running')) return 'running';
	if (statuses.includes('pending')) return 'pending';
	const last = statuses[statuses.length - 1] as InstanceRowStatus | undefined;
	if (last === undefined) return stateful ? 'idle' : undefined;
	if (!stateful) return last;
	return last === 'completed' || last === 'skipped' ? 'idle' : last;
}
