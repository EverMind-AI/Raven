import type { Msg } from '@agentscope-ai/agentscope/message';
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';

import {
	deriveInstances,
	mergeRegistryRows,
	transportOf,
	type Exchange,
	type MonitorInstance,
} from './deriveInstances';
import type { RavenDagNodeDetail, RavenSubagentInstance, RavenThirdPartySubagent } from '@/api';
import { ravenConfigApi } from '@/api';
import { useDagRuns } from '@/components/chat/DagRunsContext';
import { useSubagentInstances } from '@/components/chat/SubagentInstancesContext';
import { DeleteDialog } from '@/components/dialog/DeleteDialog';
import { Button } from '@/components/ui/button';
import type { ReplyPhase } from '@/hooks/useMessages';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import MessageSquare from '~icons/solar/chat-round-line-bold';
import Bot from '~icons/solar/cpu-bold-duotone';
import Square from '~icons/solar/stop-linear';

/** How often to poll the registry while something is actually in flight (a
 *  pending/running row, or the chat itself streaming). Idle docks never
 *  poll, so this only trades a little staleness for liveness during a run;
 *  2.5s keeps a stop button appearing promptly without hammering the RPC. */
const LIVE_POLL_MS = 2500;

/** How much of a DAG node's output to pull into an expanded exchange. Smaller
 *  than the graph's own preview: this is a narrow side panel, not the main
 *  reading surface. */
const EXCHANGE_OUTPUT_CHARS = 8000;

interface SubagentInstanceMonitorProps {
	/**
	 * Switch the chat area to this instance's direct chat. Offered for every row
	 * that names an agent, DAG-driven ones included: a fan-out registers an
	 * ordinary instance row per node beside the `dag-node` row, and that one is
	 * addressable. Gating on `runId` hid exactly those -- the rows a user is
	 * most likely to want to follow up with.
	 */
	onEnterDirect?: (target: { agent: string; handle: string }) => void;
	msgs: Msg[];
	sessionId: string | null;
	subagents: RavenThirdPartySubagent[];
	/** The chat's own reply lifecycle; used as one of the "something is live"
	 *  signals that keeps the registry polling (see the fetch effects below). */
	phase: ReplyPhase;
}

/** Text color for a status badge; unmatched statuses fall back to muted. */
function nodeStatusClass(status?: string): string {
	switch (status) {
		case 'running':
			return 'text-gold animate-pulse';
		case 'completed':
			return 'text-gold-olive';
		case 'failed':
			return 'text-destructive';
		default:
			return 'text-muted-foreground';
	}
}

/**
 * One invocation of an instance: its input and the reply it produced.
 *
 * A DAG-node exchange holds neither. The transcript has only the node's prompt
 * *template*, and its output never enters the conversation at all — it goes to
 * a file so a large result stays out of the leader's context. Both are read
 * back from the run dir here, on expand, one node at a time. A ``spawn``
 * exchange has no such record: its reply arrives as a separate announce turn
 * with no handle to tie it back to, so its output stays empty.
 */
function ExchangeCard({ exchange, sessionKey }: { exchange: Exchange; sessionKey: string }) {
	const { t } = useTranslation();
	const [detail, setDetail] = useState<RavenDagNodeDetail | null>(null);
	const [loading, setLoading] = useState(false);
	const runId = exchange.source?.runId;
	const node = exchange.source?.node;

	useEffect(() => {
		setDetail(null);
		if (!runId || !node) return;
		let cancelled = false;
		setLoading(true);
		ravenConfigApi
			.getDagNode(runId, node, EXCHANGE_OUTPUT_CHARS, sessionKey)
			.then((r) => {
				if (!cancelled) setDetail(r.node);
			})
			.catch(() => {
				// The run dir may be pruned; the template below still stands in.
			})
			.finally(() => {
				if (!cancelled) setLoading(false);
			});
		return () => {
			cancelled = true;
		};
	}, [runId, node, sessionKey]);

	// The rendered prompt is what the sub-agent actually received; the template
	// it was built from is the fallback when the run dir is unreadable.
	const prompt = detail?.prompt || exchange.prompt;
	const output = detail?.output ?? exchange.output;

	return (
		<div className="flex flex-col gap-1">
			<div className="flex items-center gap-2 px-1 text-[10px] text-muted-foreground">
				<span>{t(`subagent-monitor.action.${exchange.action}`)}</span>
				{node && <span className="min-w-0 truncate">{node}</span>}
			</div>
			{/* Input and output get different surfaces, not just a divider: they
			    were previously one box split by a border, on the same background
			    the dock itself sits on, which made a long prompt run visually
			    straight into the reply. `muted` is the recessed surface (what went
			    in), `card` the raised one (what came back); both differ from the
			    page background, and both pairs are defined for light and dark. */}
			{prompt && (
				<div className="flex flex-col rounded-sm border bg-muted">
					<div className="border-b px-2 py-1 text-[10px] text-muted-foreground">
						{t('tool.subagentPrompt')}
					</div>
					<div className="max-h-[160px] overflow-auto px-2 py-1 text-xs whitespace-pre-wrap break-words">
						{prompt}
					</div>
				</div>
			)}
			<div className="flex flex-col rounded-sm border bg-card">
				<div className="flex items-center gap-2 border-b px-2 py-1 text-[10px] text-muted-foreground">
					<span>{t('tool.subagentResponse')}</span>
					{detail?.output_truncated && (
						<span className="ml-auto">
							{t('subagent-monitor.truncated', {
								shown: EXCHANGE_OUTPUT_CHARS,
								total: detail.output_chars,
							})}
						</span>
					)}
				</div>
				<div className="max-h-[200px] overflow-auto px-2 py-1 text-xs whitespace-pre-wrap break-words">
					{output || (
						<span className="text-muted-foreground">
							{loading
								? t('subagent-monitor.loading')
								: t('subagent-monitor.noOutput')}
						</span>
					)}
				</div>
			</div>
		</div>
	);
}

/**
 * One-line subtitle for a collapsed row: what this instance was last asked to
 * do.
 *
 * Both sources are present from the moment the call is issued — they are read
 * off the call arguments — but the full text lives in the expanded detail, so
 * a row that had never been clicked said only which sub-agent it was, never
 * what it was doing. `spawn` supplies a `label` written for display; a DAG
 * node has no equivalent, so the prompt's first line stands in. CSS truncates,
 * so no length cap is needed here.
 */
function rowSummary(inst: MonitorInstance): string {
	const last = inst.exchanges[inst.exchanges.length - 1];
	if (!last) return '';
	const text = last.label || last.prompt;
	return (text.split('\n').find((line) => line.trim()) ?? '').trim();
}

/**
 * Read-only right-dock monitor of this session's sub-agent instances.
 *
 * The list is instances only — DAG *runs* are not rows here. A run's shape,
 * per-node status and timing are drawn in the conversation, inside the tool
 * call that started it, so repeating the run as a summary row said nothing the
 * graph did not already show. Its nodes are still represented, but as what they
 * are: sub-agent invocations, folded into the instance each one belongs to.
 *
 * Two sources are merged per row. The registry
 * (``~/.raven/subagent_instances.json``, via
 * `ravenConfigApi.listSubagentInstances`) is authoritative for status and
 * survives a page reload; the transcript supplies the exchange history, since
 * the registry stores only a handle-to-session-id mapping, not conversation
 * content. Either source can carry a row the other does not: a registry entry
 * with no transcript match renders with zero exchanges, and a node the run
 * never reached renders with no status.
 */
export function SubagentInstanceMonitor({
	msgs,
	onEnterDirect,
	sessionId,
	subagents,
	phase,
}: SubagentInstanceMonitorProps) {
	const { t } = useTranslation();
	const { instances: liveInstances } = useSubagentInstances();
	const { dagRuns } = useDagRuns();
	const [registry, setRegistry] = useState<RavenSubagentInstance[]>([]);

	// The gateway agent scopes a spawn to `web:<session id>`; see
	// service/raven_gateway_agent.py.
	const sessionKey = sessionId ? `web:${sessionId}` : '';

	// Clear immediately on session switch so the previous session's rows
	// don't linger on screen while the new session's registry fetch is in
	// flight.
	useEffect(() => {
		setRegistry([]);
	}, [sessionKey]);

	// Monotonic per-call token, not a per-effect `cancelled` flag: fetchRegistry
	// is now the *only* fetch path (immediate effect, poll effect, and the
	// post-cancel refetch all share it), so a boolean scoped to one effect
	// can't guard the others. A stale response is only ever a strictly older
	// generation than the latest in-flight call, whichever effect started
	// either -- including across a session switch, where an in-flight fetch
	// for the old session must not clobber (or, on rejection, blank out) rows
	// already fetched for the new one.
	const fetchGenRef = useRef(0);
	const fetchRegistry = useCallback(async () => {
		if (!sessionKey) return;
		const gen = ++fetchGenRef.current;
		try {
			const res = await ravenConfigApi.listSubagentInstances(sessionKey);
			if (fetchGenRef.current === gen) setRegistry(res.instances ?? []);
		} catch {
			if (fetchGenRef.current === gen) setRegistry([]);
		}
	}, [sessionKey]);

	// Message count is NOT a sufficient refresh trigger on its own: a DAG run
	// or a CLI spawn reports its progress entirely through CUSTOM events
	// (dag_run_started / dag_node_updated / dag_run_completed) that never
	// touch `msgs`, and all reply content folds into the single msg created
	// at REPLY_START -- so `msgs.length` only changes twice per turn, both
	// before the tool ever runs. `dagRuns` (from useDagRuns) changes on every
	// dag_node_updated, so it's a free additional liveness signal here. `phase`
	// is also a trigger: the interval-poll effect below is armed on
	// REPLY_START and ticks every LIVE_POLL_MS, so a reply that ends within
	// one interval of the spawn can go idle (tearing the poll down) before any
	// tick lands -- nothing else fetches at REPLY_END, since it appends into
	// the existing msg and never moves `msgCount`. The streaming -> idle
	// transition here is a fetch that strictly follows the (awaited) row
	// write, so it is guaranteed to observe it.
	const msgCount = msgs.length;
	useEffect(() => {
		fetchRegistry();
	}, [fetchRegistry, msgCount, dagRuns, phase]);

	// dag_node_updated does not cover CLI spawns (they emit no CUSTOM event at
	// all), so on top of the trigger above, poll on an interval while
	// something is actually in flight: any pending/running registry row, or
	// the chat reply itself streaming (covers a CLI spawn from the moment it
	// is requested, before any row can exist). Stops the instant nothing is
	// live, so an idle dock never polls.
	const anyLive =
		phase === 'streaming' ||
		registry.some((r) => r.status === 'pending' || r.status === 'running');
	useEffect(() => {
		if (!sessionKey || !anyLive) return;
		const id = setInterval(fetchRegistry, LIVE_POLL_MS);
		return () => clearInterval(id);
	}, [sessionKey, anyLive, fetchRegistry]);

	const statefulNames = useMemo(
		() =>
			new Set(
				subagents.filter((s) => s.kind === 'cli' && s.resumeCommand).map((s) => s.name),
			),
		[subagents],
	);
	const derived = useMemo(
		() => deriveInstances(msgs, statefulNames, dagRuns),
		[msgs, statefulNames, dagRuns],
	);

	const instances = useMemo(() => mergeRegistryRows(derived, registry), [derived, registry]);

	const [selected, setSelected] = useState<string | null>(null);

	const [cliStopTarget, setCliStopTarget] = useState<{ agent: string; handle: string } | null>(
		null,
	);
	const [dagStopTarget, setDagStopTarget] = useState<string | null>(null);

	/** Toast the `{cancelled}` outcome (or the request's own failure), then
	 *  refetch regardless. `result` rejecting must not propagate: it's passed
	 *  straight to `DeleteDialog`'s `onConfirm`, which has no catch of its
	 *  own, so an uncaught rejection here would leave the dialog stuck open. */
	const settleCancel = async (result: Promise<{ cancelled: boolean }>) => {
		try {
			const { cancelled } = await result;
			if (cancelled) {
				toast.success(t('subagent-monitor.stopRequested'));
			} else {
				toast.error(t('subagent-monitor.stopFailed'));
			}
		} catch {
			toast.error(t('subagent-monitor.stopFailed'));
		} finally {
			await fetchRegistry();
		}
	};

	const confirmCancelCli = () => {
		if (!cliStopTarget) return Promise.resolve();
		return settleCancel(
			ravenConfigApi.cancelSubagentInstance({
				session_key: sessionKey,
				agent: cliStopTarget.agent,
				handle: cliStopTarget.handle,
			}),
		);
	};

	const confirmCancelDag = () => {
		if (!dagStopTarget) return Promise.resolve();
		return settleCancel(ravenConfigApi.cancelDagRun(dagStopTarget));
	};

	if (instances.length === 0) {
		return <p className="p-3 text-xs text-muted-foreground">{t('subagent-monitor.empty')}</p>;
	}

	return (
		<div className="flex flex-col gap-2 p-2">
			{instances.map((inst) => {
				const live = liveInstances[inst.handle];
				const transport = live?.transport ?? transportOf(inst.prototype, subagents);
				const stoppable = inst.status === 'running' || inst.status === 'pending';
				const expanded = inst.key === selected;
				return (
					// The detail sits inside this fragment, directly under its own
					// row -- not after the whole list, which read as belonging to
					// the last row rather than the one that was clicked.
					<Fragment key={inst.key}>
						<div
							role="button"
							tabIndex={0}
							aria-expanded={expanded}
							onClick={() => setSelected(expanded ? null : inst.key)}
							onKeyDown={(e) => {
								// Ignore keydowns that bubbled up from a nested control (e.g. the
								// stop button) so activating that control doesn't also toggle the row.
								if (e.target !== e.currentTarget) return;
								if (e.key === 'Enter' || e.key === ' ') {
									e.preventDefault();
									setSelected(expanded ? null : inst.key);
								}
							}}
							className={cn(
								'flex cursor-pointer flex-col gap-0.5 rounded-sm border px-2 py-1 text-left hover:bg-accent',
								expanded && 'bg-accent',
							)}
						>
							<span className="flex w-full items-center gap-2">
							<Bot className="size-4 shrink-0" />
							<span className="min-w-0 flex-1 truncate text-sm">{inst.handle}</span>
							{onEnterDirect && inst.agent && (
								<span
									role="button"
									tabIndex={0}
									// Deliberately unlike the badges beside it: `Writer`,
									// `done`, `CLI` and `x1` are all passive status, and a
									// control styled the same way reads as a fifth label
									// nobody would think to click.
									className="flex shrink-0 items-center gap-1 rounded-full bg-primary px-2 py-0.5 text-[10px] font-medium text-primary-foreground hover:opacity-80"
									onClick={(e) => {
										// The row itself expands the detail; entering a
										// chat is a different action on the same row.
										e.stopPropagation();
										onEnterDirect({
											agent: inst.agent!,
											handle: inst.handle,
										});
									}}
									onKeyDown={(e) => {
										if (e.key === 'Enter' || e.key === ' ') {
											e.stopPropagation();
											onEnterDirect({
												agent: inst.agent!,
												handle: inst.handle,
											});
										}
									}}
								>
									<MessageSquare className="size-3" />
									{t('subagent-monitor.chat')}
								</span>
							)}
							<span className="text-xs text-muted-foreground">{inst.prototype}</span>
							{inst.status && (
								<span
									className={cn(
										'rounded-sm border px-1 text-[10px]',
										nodeStatusClass(inst.status),
									)}
								>
									{t(`subagent-monitor.nodeStatus.${inst.status}`)}
								</span>
							)}
							<span className="rounded-sm border px-1 text-[10px] text-muted-foreground">
								{t(`subagent-monitor.transport.${transport}`)}
							</span>
							<span className="text-xs text-muted-foreground">
								×{inst.exchanges.length}
							</span>
							{stoppable && (inst.runId || inst.agent) && (
								<Button
									variant="ghost"
									size="icon-xs"
									onClick={(e) => {
										e.stopPropagation();
										// Node-level cancellation isn't offered, so stopping a
										// DAG-driven instance stops its whole run; the confirm
										// dialog says so.
										if (inst.runId) setDagStopTarget(inst.runId);
										else if (inst.agent)
											setCliStopTarget({
												agent: inst.agent,
												handle: inst.handle,
											});
									}}
									aria-label={
										inst.runId
											? t('subagent-monitor.stopRun')
											: t('subagent-monitor.stopInstance')
									}
								>
									<Square className="size-3.5" />
								</Button>
							)}
							</span>
							{/* Its own full-width line rather than a second column: the
							    badges to the right are not shrinkable, so a summary
							    sharing that row was squeezed to ~74px of a 310px dock
							    -- present, but too narrow to read. */}
							{rowSummary(inst) && (
								<span className="w-full truncate text-[11px] font-normal text-muted-foreground">
									{rowSummary(inst)}
								</span>
							)}
						</div>
						{expanded && (
							// Indented and rule-joined to its row so a long exchange
							// still reads as that row's, not as a new top-level item.
							<div className="ml-3 flex flex-col gap-2 border-l pl-2">
								{inst.agentId && (
									<p className="text-[11px] text-muted-foreground">
										{t('subagent-monitor.sessionId')}: {inst.agentId}
									</p>
								)}
								{inst.exchanges.length === 0 && (
									<p className="text-[11px] text-muted-foreground">
										{t('subagent-monitor.noExchanges')}
									</p>
								)}
								{inst.exchanges.map((ex, i) => (
									<ExchangeCard key={i} exchange={ex} sessionKey={sessionKey} />
								))}
							</div>
						)}
					</Fragment>
				);
			})}
			<DeleteDialog
				open={cliStopTarget !== null}
				onOpenChange={(open) => {
					if (!open) setCliStopTarget(null);
				}}
				title={t('subagent-monitor.stopInstance')}
				description={t('subagent-monitor.stopInstanceConfirm')}
				confirmLabel={t('subagent-monitor.stopInstance')}
				onConfirm={confirmCancelCli}
			/>
			<DeleteDialog
				open={dagStopTarget !== null}
				onOpenChange={(open) => {
					if (!open) setDagStopTarget(null);
				}}
				title={t('subagent-monitor.stopRun')}
				description={t('subagent-monitor.stopRunConfirm')}
				confirmLabel={t('subagent-monitor.stopRun')}
				onConfirm={confirmCancelDag}
			/>
		</div>
	);
}
