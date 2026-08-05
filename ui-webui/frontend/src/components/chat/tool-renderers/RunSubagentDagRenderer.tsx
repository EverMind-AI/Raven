import { getResultText, parseInput, toolLabelClass } from './_shared';
import type { TFunction, ToolCallWithResult, ToolRenderer } from './types';
import { useDagRuns } from '@/components/chat/DagRunsContext';
import { DagGraph } from '@/components/dag/DagGraph';
import {
	DAG_STATUS_LABEL_KEY,
	readDagManifest,
	resolveStatus,
	toStatus,
} from '@/components/dag/deriveDag';
import type { DagSummary } from '@/components/dag/deriveDag';
import { reusedCountOf, withReusedNodes } from '@/components/dag/deriveReusedNodes';
import type { DagVizNode } from '@/components/dag/layoutDag';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import Workflow from '~icons/solar/siderbar-bold-duotone';

/** The `nodes` array as submitted in the tool call, or `[]` if unparseable. */
function callNodesOf(pair: ToolCallWithResult): Array<{
	id?: string;
	subagent?: string;
	instance?: string | null;
	depends_on?: string[];
	prompt_template?: string;
	inputs?: unknown;
}> {
	const parsed = parseInput(pair.call.input) as { nodes?: unknown[] };
	return Array.isArray(parsed.nodes) ? (parsed.nodes as ReturnType<typeof callNodesOf>) : [];
}

/** node id -> the `prompt_template` the agent submitted for it. */
function templatesOf(pair: ToolCallWithResult): Map<string, string> {
	const out = new Map<string, string>();
	for (const n of callNodesOf(pair)) {
		if (typeof n.id === 'string' && typeof n.prompt_template === 'string') {
			out.set(n.id, n.prompt_template);
		}
	}
	return out;
}

function nodeCountOf(pair: ToolCallWithResult): number {
	const manifest = readDagManifest(pair.result?.metadata);
	if (manifest) return manifest.files.length;
	return callNodesOf(pair).length;
}

/**
 * Hairline KPI strip summarizing a finalized run's node counts.
 *
 * `summary` is the runner's own, reported as-is: its `total` counts the nodes
 * this run executed and nothing else. Reused nodes are a client-side finding, so
 * they get their own chip rather than being folded into `total` — inflating a
 * backend number with a number the backend never computed would make the strip
 * disagree with the manifest on disk.
 */
function DagSummaryStrip({
	summary,
	reused,
	t,
}: {
	summary: DagSummary;
	reused: number;
	t: TFunction;
}) {
	// `[reactKey, labelKey, value]`: the react key stays the untranslated name so
	// switching language re-labels the chips in place instead of remounting them.
	const items: Array<[string, string, number]> = [
		['total', 'dag.summary.total', summary.total],
		['completed', DAG_STATUS_LABEL_KEY.completed, summary.completed],
		['failed', DAG_STATUS_LABEL_KEY.failed, summary.failed],
		['skipped', DAG_STATUS_LABEL_KEY.skipped, summary.skipped],
	];
	if (reused > 0) items.push(['reused', DAG_STATUS_LABEL_KEY.reused, reused]);
	return (
		<div className="flex items-center gap-3 rounded-md border bg-background px-3 py-1.5 text-xs">
			{items.map(([key, labelKey, value], i) => (
				<div key={key} className={cn('flex items-center gap-1', i > 0 && 'border-l pl-3')}>
					<span className="font-medium tabular-nums text-gold">{value}</span>
					<span className="text-[10px] uppercase tracking-wide text-muted-foreground">
						{t(labelKey)}
					</span>
				</div>
			))}
		</div>
	);
}

/**
 * Inner component so it can read the live-DAG context (a `renderBody` is a
 * plain function, not a React component, and cannot call hooks).
 *
 * Reconciliation is gated on manifest presence + error state, NOT on whether
 * a tool result exists. This matters because `run_subagent_dag` is offloaded
 * to a background task after ~10s (its CLI sub-agents take minutes): the
 * offload middleware lands a metadata-less "running in background" placeholder
 * as the tool result while the DAG keeps running and keeps publishing
 * `dag_node_updated` events. So:
 *   1. manifest present            -> authoritative graph (non-offloaded runs);
 *   2. else live overlay present   -> animate from the dag_* events (this is
 *      the path for offloaded runs, and for in-progress blocking runs);
 *   3. else not an error           -> planned structure from call args (pending);
 *   4. else (errored, no overlay)  -> empty, so the text fallback shows the
 *      error (e.g. "Invalid DAG: ..."; validation errors emit no dag_* events).
 *
 * Per-node ``started_at`` / ``ended_at`` annotations are layered in from
 * the live overlay first (so a still-running node shows a ticking
 * duration) and fall back to the manifest's recorded timings once the
 * run finalises (so a reload still shows the final duration).
 */
function DagBody({ pair }: { pair: ToolCallWithResult }) {
	const { t } = useTranslation();
	const { dagRuns, latestRunId, runIdByToolCallId } = useDagRuns();
	const runId = runIdByToolCallId[pair.call.id] ?? latestRunId ?? undefined;
	const live = runId ? dagRuns[runId] : undefined;
	const manifest = readDagManifest(pair.result?.metadata) ?? readDagManifest(live?.manifest);
	const isError = pair.result?.state === 'error';
	// Each node's prompt template, read off the call's own arguments. Neither
	// the manifest nor the dag_* events carry it, and the args are in the
	// transcript, so this is the one source that is always there.
	const templates = templatesOf(pair);

	let vizNodes: DagVizNode[];
	if (manifest) {
		const terminal = new Map(manifest.terminal_outputs.map((t) => [t.node, t.text]));
		const liveByNode = live?.byNode ?? {};
		const liveTimings = live?.timings ?? {};
		const manifestByNode = new Map(manifest.files.map((f) => [f.node, f]));
		const allIds = new Set<string>([
			...manifestByNode.keys(),
			...(live?.nodes.map((n) => n.id) ?? []),
		]);
		vizNodes = [...allIds].map((id) => {
			const f = manifestByNode.get(id);
			if (f) {
				const startedAt = liveTimings[id]?.startedAt ?? f.started_at ?? null;
				const endedAt = liveTimings[id]?.endedAt ?? f.ended_at ?? null;
				const durationSec =
					startedAt != null && endedAt != null
						? Math.max(0, (endedAt - startedAt) / 1000)
						: null;
				return {
					id: f.node,
					subagent: f.subagent,
					instance: f.instance ?? null,
					depends_on: f.depends_on ?? [],
					status: resolveStatus(f.status, liveByNode[id]),
					startedAt,
					durationSec,
					outputFile: f.output_file ?? null,
					promptFile: f.prompt_file ?? null,
					promptTemplate: templates.get(f.node) ?? null,
					error: f.error ?? null,
					terminalOutput: terminal.get(f.node) ?? null,
				};
			}
			// Manifest missed this id but the live overlay had it; render
			// from the live data so an intermediate render isn't blank.
			const liveNode = live?.nodes.find((n) => n.id === id);
			const status = toStatus(liveByNode[id]);
			return {
				id,
				subagent: liveNode?.subagent,
				instance: liveNode?.instance ?? null,
				depends_on: liveNode?.depends_on ?? [],
				status,
				startedAt: liveTimings[id]?.startedAt ?? null,
				durationSec:
					liveTimings[id]?.startedAt != null && liveTimings[id]?.endedAt != null
						? Math.max(
								0,
								(liveTimings[id]!.endedAt! - liveTimings[id]!.startedAt!) / 1000,
							)
						: null,
				promptTemplate: templates.get(id) ?? null,
			};
		});
	} else if (live && live.nodes.length > 0) {
		// Live overlay from dag_* events — the path for offloaded runs (whose
		// tool result is a metadata-less placeholder) and in-progress runs.
		const liveTimings = live.timings ?? {};
		vizNodes = live.nodes.map((n) => ({
			id: n.id,
			subagent: n.subagent,
			instance: n.instance ?? null,
			depends_on: n.depends_on ?? [],
			status: toStatus(live.byNode[n.id]),
			startedAt: liveTimings[n.id]?.startedAt ?? null,
			durationSec:
				liveTimings[n.id]?.startedAt != null && liveTimings[n.id]?.endedAt != null
					? Math.max(
							0,
							(liveTimings[n.id]!.endedAt! - liveTimings[n.id]!.startedAt!) / 1000,
						)
					: null,
			promptTemplate: templates.get(n.id) ?? null,
		}));
	} else if (!isError) {
		// No manifest, no overlay yet, not an error: draw the planned structure
		// from the (fully-streamed) call args as all-pending.
		vizNodes = callNodesOf(pair)
			.filter((n) => typeof n.id === 'string')
			.map((n) => ({
				id: n.id as string,
				subagent: n.subagent,
				instance: n.instance ?? null,
				depends_on: Array.isArray(n.depends_on) ? n.depends_on : [],
				status: 'pending' as const,
				promptTemplate: n.prompt_template ?? null,
			}));
	} else {
		// Errored result with no manifest/overlay (e.g. Invalid DAG): leave
		// empty so the getResultText fallback surfaces the error text.
		vizNodes = [];
	}

	if (vizNodes.length === 0) {
		const text = getResultText(pair.result);
		return text ? (
			<pre className="whitespace-pre-wrap text-xs">{text}</pre>
		) : (
			<p className="text-xs text-muted-foreground">Preparing DAG…</p>
		);
	}
	// Applied once, after every branch above, so a retry DAG that reads an
	// earlier run's outputs shows those upstream nodes whether it is finalized,
	// live, or still only planned. Only the call args carry the cross-run
	// references, which is why this cannot live in any of the branches' sources.
	const withReused = withReusedNodes(vizNodes, callNodesOf(pair), runId, dagRuns);
	return (
		<>
			{manifest?.summary && (
				<DagSummaryStrip
					summary={manifest.summary}
					reused={reusedCountOf(withReused)}
					t={t}
				/>
			)}
			<DagGraph nodes={withReused} runId={runId} />
		</>
	);
}

/**
 * Inline, always-open card for the ``run_subagent_dag`` tool call.
 *
 * Renders the DAG header + live graph directly in the conversation flow —
 * without the collapsible ``ToolCallRow`` shell — so the visualisation is
 * always visible and can never be folded away. Used by ``MessageBubble``
 * once the call is extracted out of the tool-chain group summary (see
 * ``INLINE_TOOL_NAMES``); the DAG graph is the whole point of the call, so
 * the user should see it without an extra click.
 */
export function RunSubagentDagInlineCard({ pair, t }: { pair: ToolCallWithResult; t: TFunction }) {
	const count = nodeCountOf(pair);
	return (
		<div className="flex flex-col gap-2 rounded-md border border-border bg-card p-3">
			<div className="flex items-center gap-2">
				<Workflow className="size-3.5 shrink-0 text-foreground/80" />
				<span
					className={cn(
						toolLabelClass,
						'text-gold uppercase tracking-wide text-xs font-medium',
					)}
				>
					{count > 0 ? t('tool.subagentDagTitle', { count }) : t('tool.subagentDag')}
				</span>
			</div>
			<DagBody pair={pair} />
		</div>
	);
}

export const RunSubagentDagRenderer: ToolRenderer = {
	getDisplayName: (_call, t) => t('tool.subagentDag'),

	renderHeader: (pair, t) => {
		const count = nodeCountOf(pair);
		return (
			<span className={toolLabelClass}>
				{count > 0 ? t('tool.subagentDagTitle', { count }) : t('tool.subagentDag')}
			</span>
		);
	},

	renderBody: (pair) => <DagBody pair={pair} />,
};
