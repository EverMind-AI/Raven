import {
	Background,
	ControlButton,
	Controls,
	Handle,
	MiniMap,
	Position,
	ReactFlow,
	useEdgesState,
	useNodesState,
} from '@xyflow/react';
import type { Edge, NodeChange, NodeProps, XYPosition } from '@xyflow/react';
import { useTheme } from 'next-themes';
import type { ReactNode } from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { DAG_STATUS_LABEL_KEY } from './deriveDag';
import type { DagNodeStatus } from './deriveDag';
import { layoutDag } from './layoutDag';
import type { DagFlowNode, DagVizNode } from './layoutDag';
import type { RavenDagNodeDetail } from '@/api';
import { ravenConfigApi } from '@/api';
import { useDagRuns } from '@/components/chat/DagRunsContext';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import CheckCircle2 from '~icons/solar/check-circle-bold-duotone';
import XCircle from '~icons/solar/close-circle-bold-duotone';
import TriangleAlert from '~icons/solar/danger-triangle-bold-duotone';
import MinusCircle from '~icons/solar/minus-circle-bold-duotone';
import Circle from '~icons/solar/record-circle-linear';
import LoaderCircle from '~icons/solar/refresh-linear';
import Restart from '~icons/solar/restart-linear';

import '@xyflow/react/dist/style.css';

const STATUS_STYLE: Record<DagNodeStatus, { box: string; icon: ReactNode }> = {
	pending: {
		box: 'border-border text-muted-foreground',
		icon: <Circle className="size-3 shrink-0" />,
	},
	running: {
		box: 'border-gold text-gold',
		icon: <LoaderCircle className="size-3 shrink-0 animate-spin" />,
	},
	completed: {
		box: 'border-gold-olive text-gold-olive',
		icon: <CheckCircle2 className="size-3 shrink-0" />,
	},
	failed: {
		box: 'border-destructive text-destructive',
		icon: <XCircle className="size-3 shrink-0" />,
	},
	skipped: {
		box: 'border-dashed border-border text-muted-foreground opacity-60',
		icon: <MinusCircle className="size-3 shrink-0" />,
	},
	cancelled: {
		box: 'border-destructive/70 text-destructive/90',
		icon: <MinusCircle className="size-3 shrink-0" />,
	},
	exception: {
		box: 'border-amber-500/40 text-amber-600 dark:text-amber-400',
		icon: <TriangleAlert className="size-3 shrink-0" />,
	},
	interrupted: {
		box: 'border-dashed border-destructive/60 text-destructive/80',
		icon: <MinusCircle className="size-3 shrink-0" />,
	},
	reused: {
		box: 'border-dashed border-gold-olive/50 text-gold-olive/70 opacity-75',
		icon: <Restart className="size-3 shrink-0" />,
	},
};

/** Format a duration in seconds as a short ``X.Xs`` / ``Xm Ys`` string. */
function formatDuration(seconds: number): string {
	if (seconds < 60) return `${seconds.toFixed(1)}s`;
	const m = Math.floor(seconds / 60);
	const s = Math.floor(seconds % 60);
	return `${m}m ${s}s`;
}

/**
 * Per-node running-time badge.
 *
 * Renders an empty (but still full-width) slot when no duration info is
 * available — e.g. a finished node loaded from manifest on page reload, with no
 * live overlay timestamps. When the node is still running, ticks every 100 ms
 * so the tenths-of-a-second display updates smoothly in real time. This tick
 * re-renders only this leaf badge — it is isolated inside the React Flow node,
 * so it never recreates the graph's node/edge arrays and cannot cause the
 * canvas to reflow.
 *
 * The slot is a fixed-width, right-aligned, `tabular-nums` box: the text grows
 * and shrinks as the count crosses 9.9s / 59.9s / 9m, and letting that resize
 * the badge made the whole node twitch once per tick.
 */
function DagDuration({
	status,
	durationSec,
	startedAt,
}: {
	status: DagNodeStatus;
	durationSec?: number | null;
	startedAt?: number | null;
}) {
	const [now, setNow] = useState(() => Date.now());

	useEffect(() => {
		if (status !== 'running' || !startedAt) return;
		const id = window.setInterval(() => setNow(Date.now()), 100);
		return () => window.clearInterval(id);
	}, [status, startedAt]);

	let text = '';
	if (status === 'running' && startedAt) {
		text = formatDuration(Math.max(0, (now - startedAt) / 1000));
	} else if (typeof durationSec === 'number') {
		text = formatDuration(durationSec);
	}

	return (
		<span className="w-14 shrink-0 text-right text-[10px] tabular-nums text-muted-foreground">
			{text}
		</span>
	);
}

/**
 * One graph node.
 *
 * Deliberately a fixed width and a fixed number of rows: the duration badge
 * ticks ten times a second, so anything whose size depends on it — a
 * `min-w` box that grows with its content, a label sharing the row — visibly
 * jitters for the whole run. The label gets a row to itself, the duration sits
 * in its own fixed slot on the meta row, and the box never changes size.
 */
function DagStatusNode({ data }: NodeProps<DagFlowNode>) {
	const { t } = useTranslation();
	const style = STATUS_STYLE[data.status];
	const reusedFrom = data.reusedFrom ?? null;
	return (
		<div
			className={cn(
				'icon-mono w-[180px] rounded-md border bg-background px-3 py-2 text-xs',
				style.box,
			)}
			title={
				reusedFrom
					? t('dag.reusedFromNode', {
							run: reusedFrom.runId,
							node: reusedFrom.nodeId,
						})
					: t(DAG_STATUS_LABEL_KEY[data.status])
			}
		>
			<Handle type="target" position={Position.Left} className="!bg-muted-foreground" />
			<div className="flex items-center gap-1.5">
				{style.icon}
				<span
					className={cn(
						'min-w-0 flex-1 truncate font-medium',
						reusedFrom ? 'text-muted-foreground' : 'text-foreground',
					)}
				>
					{data.label}
				</span>
			</div>
			<div className="mt-0.5 flex items-center gap-1.5">
				<span className="min-w-0 flex-1 truncate text-[10px] text-muted-foreground">
					{data.subagent}
				</span>
				<DagDuration
					status={data.status}
					durationSec={data.durationSec ?? null}
					startedAt={data.startedAt ?? null}
				/>
			</div>
			<Handle type="source" position={Position.Right} className="!bg-muted-foreground" />
		</div>
	);
}

// Defined at module scope so the reference is stable across renders (React
// Flow warns when nodeTypes is recreated each render).
const nodeTypes = { dagNode: DagStatusNode };

/** How much of a node's output file to pull for the detail pane. The file
 *  itself is uncapped and can be megabytes; this is a preview, not the file. */
const OUTPUT_PREVIEW_CHARS = 20000;

/** A labelled, scrollable text block in the node detail. */
function DetailBlock({ label, text, tone }: { label: string; text: string; tone?: 'error' }) {
	return (
		<div className="flex flex-col gap-0.5">
			<span className="text-[10px] uppercase tracking-wide text-muted-foreground">
				{label}
			</span>
			<pre
				className={cn(
					'max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-muted p-2',
					tone === 'error' && 'text-destructive',
				)}
			>
				{text}
			</pre>
		</div>
	);
}

/**
 * The panel shown under the canvas when a node is clicked.
 *
 * The node's prompt template comes from the tool call's own arguments, so it is
 * there immediately and survives a reload. Its output does not: only leaf nodes
 * get their text inlined into the manifest's `terminal_outputs`, and the full
 * text lives in the run dir. So the output (and the *rendered* prompt, which
 * shows what a dependency actually injected in place of a `{{ x.output }}`) is
 * fetched on demand from `raven.subagents.dag.node` — per node, on click, never
 * eagerly for the whole graph.
 *
 * A reused node is read out of the run it came *from*, not the run being
 * displayed: its files only exist in the origin run's dir, and the endpoint is
 * already run-id-parameterised, so the same fetch serves both.
 */
function DagNodeDetail({ node, runId }: { node: DagVizNode; runId?: string }) {
	const { t } = useTranslation();
	const [detail, setDetail] = useState<RavenDagNodeDetail | null>(null);
	const [loading, setLoading] = useState(false);
	const { sessionKey } = useDagRuns();
	const sourceRunId = node.reusedFrom?.runId ?? runId;
	const sourceNodeId = node.reusedFrom?.nodeId ?? node.id;

	useEffect(() => {
		setDetail(null);
		if (!sourceRunId) return;
		let cancelled = false;
		setLoading(true);
		ravenConfigApi
			.getDagNode(sourceRunId, sourceNodeId, OUTPUT_PREVIEW_CHARS, sessionKey)
			.then((r) => {
				if (!cancelled) setDetail(r.node);
			})
			.catch(() => {
				// The run dir may be gone (pruned workspace, older run). The
				// manifest-inlined terminal output below is the fallback.
			})
			.finally(() => {
				if (!cancelled) setLoading(false);
			});
		return () => {
			cancelled = true;
		};
	}, [sourceRunId, sourceNodeId, sessionKey]);

	// Only worth showing next to the template when substitution changed it;
	// for a node with no placeholders the two are the same text.
	const rendered = detail?.prompt && detail.prompt !== node.promptTemplate ? detail.prompt : null;
	const output = detail?.output ?? node.terminalOutput ?? null;

	return (
		<div className="flex flex-col gap-2 rounded-md border bg-background p-3 text-xs">
			<div className="flex items-center gap-2">
				<span className="font-medium text-foreground">{sourceNodeId}</span>
				<span className="text-muted-foreground">
					{t(DAG_STATUS_LABEL_KEY[node.status])}
				</span>
				{node.subagent && <span className="text-muted-foreground">· {node.subagent}</span>}
				{node.instance && (
					<span className="rounded-sm border px-1 text-[10px] text-muted-foreground">
						{node.instance}
					</span>
				)}
			</div>
			{node.reusedFrom && (
				<div className="text-muted-foreground">
					{t('dag.reusedFrom')}: {node.reusedFrom.runId}
				</div>
			)}
			{node.depends_on.length > 0 && (
				<div className="text-muted-foreground">
					{t('dag.dependsOn')}: {node.depends_on.join(', ')}
				</div>
			)}
			{node.promptTemplate && (
				<DetailBlock label={t('dag.promptTemplate')} text={node.promptTemplate} />
			)}
			{rendered && <DetailBlock label={t('dag.renderedPrompt')} text={rendered} />}
			{node.error && <DetailBlock label={t('dag.error')} text={node.error} tone="error" />}
			{output !== null && (
				<DetailBlock
					label={
						detail?.output_truncated
							? t('dag.outputTruncated', {
									shown: OUTPUT_PREVIEW_CHARS,
									total: detail.output_chars,
								})
							: t('dag.output')
					}
					text={output}
				/>
			)}
			{output === null && (
				<p className="text-muted-foreground">
					{loading ? t('dag.loadingOutput') : t('dag.noOutput')}
				</p>
			)}
			{node.outputFile && (
				<div className="truncate text-[10px] text-muted-foreground">{node.outputFile}</div>
			)}
		</div>
	);
}

/**
 * Interactive DAG graph rendered inside a tool-call box. Nodes are laid out
 * by dependency depth; scroll-zoom is disabled so the canvas never hijacks
 * page scroll. Clicking a node reveals its detail below the canvas.
 */
export function DagGraph({ nodes, runId }: { nodes: DagVizNode[]; runId?: string }) {
	const { t } = useTranslation();
	const { resolvedTheme } = useTheme();
	const [selectedId, setSelectedId] = useState<string | null>(null);
	// `layoutDag` is a pure function of each node's structural + status/timing
	// fields. Key the re-layout on a signature of exactly those fields rather
	// than on the `nodes` array identity: the DagBody rebuilds `nodes` on every
	// render, and ancestor re-renders (e.g. the message bubble's 1 s
	// elapsed-time tick, or any live-overlay context update) would otherwise
	// recompute the layout and hand React Flow brand-new node/edge objects
	// each time — resetting its internal store and making the canvas flicker.
	// A running node's signature is stable (its ticking duration is rendered
	// internally by `DagDuration`, not pushed through here), so the graph
	// only re-lays-out on a genuine status/timing change.
	const signature = JSON.stringify(
		nodes.map((n) => [
			n.id,
			n.status,
			n.startedAt ?? null,
			n.durationSec ?? null,
			n.subagent ?? null,
			n.depends_on ?? [],
			n.reuseDeps ?? [],
		]),
	);

	// eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on `signature`, a stable digest of `nodes`
	const laid = useMemo(() => layoutDag(nodes), [signature]);

	// React Flow owns the node array from here on. A controlled `nodes` prop
	// with no `onNodesChange` is read-only by design: v12's `triggerNodeChanges`
	// only writes a drag back into its store when the flow was seeded with
	// `defaultNodes`, so otherwise it computes the new position and drops it --
	// which is why `nodesDraggable` alone let a node be grabbed but never moved.
	// Seeded from `laid` rather than empty so the first render already has the
	// graph and `fitView` has something to frame.
	const [rfNodes, setRfNodes, onNodesChange] = useNodesState<DagFlowNode>(laid.rfNodes);
	const [rfEdges, setRfEdges] = useEdgesState<Edge>(laid.rfEdges);

	// Where the user has put a node by hand. Held in a ref, not state, because
	// it must survive the re-layout below without being one of its inputs.
	const pinnedRef = useRef(new Map<string, XYPosition>());

	const handleNodesChange = useCallback(
		(changes: NodeChange<DagFlowNode>[]) => {
			for (const change of changes) {
				if (change.type === 'position' && change.position) {
					pinnedRef.current.set(change.id, change.position);
				}
			}
			onNodesChange(changes);
		},
		[onNodesChange],
	);

	// Re-layout on a genuine graph change, then re-pin: a node the user moved
	// keeps its position and only its `data` (status, timing) is refreshed.
	// Without the pin, every status transition during a run would snap the
	// whole arrangement back to the computed layout.
	useEffect(() => {
		setRfNodes(
			laid.rfNodes.map((n) => {
				const pinned = pinnedRef.current.get(n.id);
				return pinned ? { ...n, position: pinned } : n;
			}),
		);
		setRfEdges(laid.rfEdges);
	}, [laid, setRfNodes, setRfEdges]);

	const resetLayout = useCallback(() => {
		pinnedRef.current.clear();
		setRfNodes(laid.rfNodes);
	}, [laid, setRfNodes]);

	const selected = selectedId ? (nodes.find((n) => n.id === selectedId) ?? null) : null;

	return (
		<div className="flex flex-col gap-2">
			<div className="h-80 w-full overflow-hidden rounded-md border border-border bg-muted">
				<ReactFlow
					nodes={rfNodes}
					edges={rfEdges}
					onNodesChange={handleNodesChange}
					nodeTypes={nodeTypes}
					fitView
					fitViewOptions={{ padding: 0.2 }}
					nodesConnectable={false}
					nodesDraggable
					elementsSelectable
					zoomOnScroll={false}
					panOnScroll={false}
					zoomOnDoubleClick={false}
					minZoom={0.2}
					maxZoom={1.5}
					colorMode={resolvedTheme === 'dark' ? 'dark' : 'light'}
					proOptions={{ hideAttribution: true }}
					onNodeClick={(_, node) => setSelectedId(node.id)}
					onPaneClick={() => setSelectedId(null)}
				>
					<Background gap={16} />
					<Controls showInteractive={false}>
						{/* Dragging is only usable with a way back out of a mess. */}
						<ControlButton onClick={resetLayout} title={t('dag.resetLayout')}>
							<Restart />
						</ControlButton>
					</Controls>
					{nodes.length > 6 && <MiniMap pannable zoomable />}
				</ReactFlow>
			</div>
			{selected && <DagNodeDetail node={selected} runId={runId} />}
		</div>
	);
}
