import type { PermissionContext } from '@agentscope-ai/agentscope/permission';
import type { TaskContext } from '@agentscope-ai/agentscope/state';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';

import type {
	ChatModelConfig,
	MCPClient,
	RavenThirdPartySubagent,
	SessionKnowledgeConfig,
	TTSModelConfig,
} from '@/api';
import { ravenConfigApi, ravenSessionModelApi, ravenSessionWorkdirApi, sessionApi } from '@/api';
import MCPSvg from '@/assets/images/mcp.svg?react';
import { ChatContent } from '@/components/chat/ChatContent.tsx';
import { DagRunsContext } from '@/components/chat/DagRunsContext';
import type { DagRunLive, DagRunLiveNode } from '@/components/chat/DagRunsContext';
import { SubagentHitlCard } from '@/components/chat/SubagentHitlCard';
import { SubagentInstancesContext } from '@/components/chat/SubagentInstancesContext';
import type { InstanceStatus } from '@/components/chat/SubagentInstancesContext';
import { SubagentNamesContext } from '@/components/chat/SubagentNamesContext';
import { WorkingDirectoryControl } from '@/components/chat/WorkingDirectoryControl';
import { buildRunIdByToolCallId } from '@/components/dag/deriveDag';
import { dagRunIdsOf, mergeRestoredRuns } from '@/components/dag/restoreDagRuns';
import { DeliverablesPanel } from '@/components/delivery/DeliverablesPanel';
import { deriveDeliverables } from '@/components/delivery/deriveDeliverables';
import { KnowledgeBasePanel } from '@/components/panel/KnowledgeBasePanel';
import { McpPanel, toRavenMcpServer } from '@/components/panel/McpPanel';
import { PanelDock, type PanelDescriptor, type PanelKey } from '@/components/panel/PanelDock.tsx';
import { PermissionPanel } from '@/components/panel/PermissionPanel';
import { SkillPanel, type InjectedSkillEntry } from '@/components/panel/SkillPanel';
import { TaskPanel } from '@/components/panel/TaskPanel';
import { KnowledgeBaseParametersPopover } from '@/components/popover/KnowledgeBaseParametersPopover';
import { ModelParametersPopover } from '@/components/popover/ModelParametersPopover';
import { LlmSelect } from '@/components/select/LlmSelect';
import { PermissionModeSelect } from '@/components/select/PermissionModeSelect.tsx';
import { SubagentInstanceMonitor } from '@/components/subagent/SubagentInstanceMonitor';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
	DropdownMenu,
	DropdownMenuCheckboxItem,
	DropdownMenuContent,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
	ResizableHandle,
	ResizablePanel,
	ResizablePanelGroup,
} from '@/components/ui/resizable.tsx';
import { SidebarTrigger } from '@/components/ui/sidebar';
import { useDirectChat } from '@/hooks/useDirectChat';
import { useKnowledgeBaseMiddlewareSchema } from '@/hooks/useKnowledgeBaseMiddlewareSchema';
import { useKnowledgeBases } from '@/hooks/useKnowledgeBases';
import { useMessages } from '@/hooks/useMessages';
import { useRavenMcps } from '@/hooks/useRavenMcps';
import { useRavenModels } from '@/hooks/useRavenModels';
import { useRavenSkills } from '@/hooks/useRavenSkills';
import { useSessions } from '@/hooks/useSessions';
import { useTranslation } from '@/i18n/useI18n';
import { formatApiErrorForAlert } from '@/lib/api-error';
import ChevronDown from '~icons/solar/alt-arrow-down-linear';
import BookText from '~icons/solar/book-2-bold-duotone';
import Package from '~icons/solar/box-bold-duotone';
import ListTodo from '~icons/solar/checklist-minimalistic-bold-duotone';
import Bot from '~icons/solar/cpu-bold-duotone';
import Database from '~icons/solar/database-bold-duotone';
import ShieldCheck from '~icons/solar/shield-check-bold-duotone';
import PanelRight from '~icons/solar/siderbar-linear';

interface ChatViewportProps {
	/**
	 * The agent that owns the session being viewed. May be the
	 * user-facing leader agent or — when drilled into a team member
	 * via the URL's `:memberId` slot — a worker agent.
	 */
	agentId: string | null;
	/**
	 * The session whose messages, model config, permission mode, and
	 * workspace drive every control rendered here.
	 */
	sessionId: string | null;
	/**
	 * CLI sub-agent prototypes (user-global), supplied by the outer chat
	 * page. Used here to flag which tool calls are sub-agents and to drive
	 * the instance monitor's stateful-prototype filter; prototype CRUD
	 * itself lives on the `/subagents` page.
	 */
	subagents: RavenThirdPartySubagent[];
	/**
	 * Prototype names the gateway reports as stateful (the `statefulNames`
	 * sibling key on `/raven/subagents`), passed through to `SubagentInstanceMonitor`.
	 * `undefined` when the connected gateway predates that key, in which case
	 * the monitor falls back to its own cli-only guess from `subagents`.
	 */
	statefulNames?: string[];
	/**
	 * Optional hook invoked when a team membership change arrives on
	 * this viewport's SSE stream. The outer page owns the session list
	 * that backs the team sidebar, so it must be told to refetch too;
	 * passing this callback wires that signal up.
	 */
	onTeamUpdated?: () => void;
}

/** Maximum number of panels stacked in a single dock column. */
const MAX_PANELS_PER_COLUMN = 2;

/** How often to re-read a DAG run's durable state while it is still in flight.
 *  Only armed after a reload catches a run mid-execution -- the live dag_*
 *  events cover the normal case -- so this never runs on an idle session. */
const DAG_RESTORE_POLL_MS = 3000;

/**
 * AgentScope rejects a turn whose session has no `chat_model_config`, and that
 * check runs before the gateway-mode `get_model` shim can intervene. This
 * satisfies it; nothing ever resolves it -- the model that runs the turn is the
 * session's, held by raven.
 */
const AGENTSCOPE_MODEL_PLACEHOLDER: ChatModelConfig = {
	type: '',
	credential_id: 'raven',
	model: 'raven',
	parameters: {},
};

/**
 * Per-session localStorage key for the accumulated SkillForge-injected
 * skills. ``skills_injected`` events only stream live, so without this
 * the "skills used in this conversation" panel would reset to empty on
 * every page refresh; we persist the list and rehydrate it on load.
 */
const injectedSkillsKey = (sessionId: string) => `raven:injected-skills:${sessionId}`;

/** Hub-injected skills arrive as ``hub/<uuid>``; detect the uuid so we can
 *  resolve it to a human name via the hub detail endpoint. */
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Read the persisted injected-skills list for a session (``[]`` on miss). */
const readInjectedSkills = (sessionId: string | null | undefined): InjectedSkillEntry[] => {
	if (!sessionId) return [];
	try {
		const raw = localStorage.getItem(injectedSkillsKey(sessionId));
		return raw ? (JSON.parse(raw) as InjectedSkillEntry[]) : [];
	} catch {
		return [];
	}
};

/**
 * Insert a panel into the dock layout. Scans columns left to right and
 * appends to the first one with spare room; if every column is full a
 * new rightmost column is created. No-op when the panel is already
 * open.
 *
 * @param layout - The current column/panel arrangement.
 * @param key - The panel to open.
 * @returns A new layout array (the input is never mutated).
 */
function openPanelInLayout(layout: PanelKey[][], key: PanelKey): PanelKey[][] {
	if (layout.some((column) => column.includes(key))) return layout;
	const targetIndex = layout.findIndex((column) => column.length < MAX_PANELS_PER_COLUMN);
	if (targetIndex === -1) return [...layout, [key]];
	return layout.map((column, index) => (index === targetIndex ? [...column, key] : column));
}

/**
 * Remove a panel from the dock layout, dropping its column entirely if
 * it becomes empty.
 *
 * @param layout - The current column/panel arrangement.
 * @param key - The panel to close.
 * @returns A new layout array (the input is never mutated).
 */
function closePanelInLayout(layout: PanelKey[][], key: PanelKey): PanelKey[][] {
	return layout
		.map((column) => column.filter((panelKey) => panelKey !== key))
		.filter((column) => column.length > 0);
}

/**
 * The right-hand main panel of the chat page — every UI element that
 * operates on a single `(agentId, sessionId)` pair lives here:
 * model selector, permission mode select, message stream, workspace
 * drawer, and the team sidebar.
 *
 * Self-contained by design. The outer page passes in the
 * `(agentId, sessionId)` it wants displayed (which may be the leader
 * session or a focused team member's session) and this component
 * does the rest — fetching the session view, syncing local UI state
 * with it, and writing changes back to the same session. Switching
 * between leader and member is just a prop change; no internal
 * branching is needed.
 *
 * @param agentId - The agent to operate on. `null` while no agent is
 *   selected yet (renders an empty / disabled state).
 * @param sessionId - The session to operate on. `null` while no
 *   session is selected yet.
 * @returns The right-side main JSX of the chat page.
 */
/** The text of a composer payload; a direct chat carries no attachments yet. */
function contentText(content: { type?: string; text?: string }[]): string {
	return content
		.filter((b) => b.type === 'text' && b.text)
		.map((b) => b.text)
		.join('\n');
}

export function ChatViewport({
	agentId,
	sessionId,
	subagents,
	statefulNames,
	onTeamUpdated,
}: ChatViewportProps) {
	const { t } = useTranslation();
	const { sessions, refetch: refetchSessions } = useSessions(agentId);
	const { groups: ravenGroups, loading: ravenLoading } = useRavenModels();

	// When the viewport agent differs from the outer page's selected
	// agent (i.e. user drilled into a team member), `refetchSessions`
	// only refreshes the member's session list. The team sidebar is
	// driven by the leader's session list owned by the outer page, so
	// we also fire the parent's refetch to keep that in sync.
	const handleTeamUpdated = useCallback(() => {
		refetchSessions();
		onTeamUpdated?.();
	}, [refetchSessions, onTeamUpdated]);

	const [selectedModel, setSelectedModel] = useState<ChatModelConfig | null>(null);
	// raven's `agents.defaults.model` — what the session runs on until the user
	// picks a model. Shown as the picker's label so an unpinned session names the
	// model it actually uses instead of reading as "nothing selected".
	const [defaultModel, setDefaultModel] = useState<string | null>(null);
	const [selectedFallbackModel, setSelectedFallbackModel] = useState<ChatModelConfig | null>(
		null,
	);
	const [selectedTTSModel, setSelectedTTSModel] = useState<TTSModelConfig | null>(null);
	const [selectedKnowledgeConfig, setSelectedKnowledgeConfig] =
		useState<SessionKnowledgeConfig | null>(null);
	const [selectedPermissionMode, setSelectedPermissionMode] = useState<string>('default');
	const [selectedWorkDir, setSelectedWorkDir] = useState<string | null>(null);
	const [workDirError, setWorkDirError] = useState<string | null>(null);
	const [tasksContext, setTasksContext] = useState<TaskContext | null>(null);
	const [permissionContext, setPermissionContext] = useState<PermissionContext | null>(null);
	// Dock layout: columns laid out left→right, each holding up to 2
	// panels stacked top→bottom. Open order determines placement.
	const [panelLayout, setPanelLayout] = useState<PanelKey[][]>([]);
	const [dagRuns, setDagRuns] = useState<Record<string, DagRunLive>>({});
	const [latestDagRunId, setLatestDagRunId] = useState<string | null>(null);
	// Re-read the durable DAG run dirs while a restored run is still in flight
	// (see the restore effect below); idle sessions never poll.
	const [dagRestoreTick, setDagRestoreTick] = useState(0);
	const [dagRunRestorePending, setDagRunRestorePending] = useState(false);
	const [subagentInstances, setSubagentInstances] = useState<Record<string, InstanceStatus>>({});
	// SkillForge-injected skills accumulated across this conversation's turns
	// (from ``skills_injected`` custom events). Deduped by id; count = #turns.
	const [injectedSkills, setInjectedSkills] = useState<InjectedSkillEntry[]>([]);

	const handleStateUpdated = useCallback((value: Record<string, unknown>) => {
		if (value.tasks_context) {
			setTasksContext(value.tasks_context as TaskContext);
		}
		if (value.permission_context) {
			setPermissionContext(value.permission_context as PermissionContext);
		}
	}, []);

	const handleDagRunStarted = useCallback((value: Record<string, unknown>) => {
		const runId = value.run_id as string | undefined;
		if (!runId) return;
		const rawNodes = Array.isArray(value.nodes) ? (value.nodes as DagRunLiveNode[]) : [];
		setDagRuns((prev) => ({
			...prev,
			[runId]: {
				nodes: rawNodes,
				byNode: Object.fromEntries(rawNodes.map((n) => [n.id, 'pending'])),
				timings: {},
				created_at: (value.created_at as string) ?? '',
			},
		}));
		setLatestDagRunId(runId);
	}, []);

	const handleDagRunCompleted = useCallback((value: Record<string, unknown>) => {
		const runId = value.run_id as string | undefined;
		if (!runId) return;
		setDagRuns((prev) => {
			const existing = prev[runId] ?? { nodes: [], byNode: {} };
			return {
				...prev,
				[runId]: {
					...existing,
					manifest: value.manifest as Record<string, unknown> | undefined,
				},
			};
		});
	}, []);

	const handleDagNodeUpdated = useCallback((value: Record<string, unknown>) => {
		const runId = value.run_id as string | undefined;
		const node = value.node as string | undefined;
		const status = value.status as string | undefined;
		if (!runId || !node || !status) return;
		const startedAt = typeof value.started_at === 'number' ? value.started_at : null;
		const endedAt = typeof value.ended_at === 'number' ? value.ended_at : null;
		setDagRuns((prev) => {
			const existing = prev[runId] ?? { nodes: [], byNode: {}, timings: {} };
			const existingTimings = existing.timings ?? {};
			const prevNodeTiming = existingTimings[node] ?? {};
			const nextTiming = {
				startedAt: prevNodeTiming.startedAt ?? startedAt ?? undefined,
				endedAt: prevNodeTiming.endedAt ?? endedAt ?? undefined,
			};
			return {
				...prev,
				[runId]: {
					...existing,
					byNode: { ...existing.byNode, [node]: status },
					timings: { ...existingTimings, [node]: nextTiming },
				},
			};
		});
		setLatestDagRunId(runId);
	}, []);

	const handleSubagentInstanceUpdated = useCallback((value: Record<string, unknown>) => {
		const handle = value.handle as string | undefined;
		if (!handle) return;
		setSubagentInstances((prev) => ({
			...prev,
			[handle]: {
				status: (value.status as string) ?? 'running',
				transport: (value.transport as string) ?? 'cli',
				agentId: value.agent_id as string | undefined,
				prototype: (value.prototype as string) ?? '',
				action: value.action as string | undefined,
			},
		}));
	}, []);

	const handleSkillsInjected = useCallback(
		(value: Record<string, unknown>) => {
			const incoming = Array.isArray(value.skills)
				? (value.skills as {
						id?: string;
						source?: string;
						name?: string;
						kind?: InjectedSkillEntry['kind'];
					}[])
				: [];
			if (!incoming.length) return;
			setInjectedSkills((prev) => {
				const byId = new Map(prev.map((s) => [s.id, s]));
				for (const s of incoming) {
					const id = s.id ?? '';
					if (!id) continue;
					const existing = byId.get(id);
					if (existing) {
						// A later event carries the better-known origin: a skill first
						// seen as injected and later read (or vice versa) keeps the
						// non-empty source and the newest kind.
						byId.set(id, {
							...existing,
							count: existing.count + 1,
							source: s.source || existing.source,
							kind: s.kind ?? existing.kind,
						});
					} else {
						byId.set(id, {
							id,
							source: s.source ?? '',
							name: s.name ?? id,
							count: 1,
							kind: s.kind,
						});
					}
				}
				const next = Array.from(byId.values());
				// Persist so the panel survives a page refresh (events are live-only).
				if (sessionId) {
					try {
						localStorage.setItem(injectedSkillsKey(sessionId), JSON.stringify(next));
					} catch {
						/* ignore quota / serialization errors */
					}
				}
				return next;
			});
		},
		[sessionId],
	);

	// Resolve hub-injected skills shown as a bare "hub/<uuid>" to a readable
	// name (covers both live events and entries rehydrated from localStorage).
	// Converges: once a name is patched in it is no longer a uuid, so the
	// filter empties and the effect stops firing.
	useEffect(() => {
		const pending = injectedSkills.filter((s) => s.source === 'hub' && UUID_RE.test(s.name));
		if (!pending.length) return;
		let cancelled = false;
		for (const s of pending) {
			void ravenConfigApi.skills.hub
				.skill(s.name)
				.then((d) => {
					if (cancelled || !d?.name || d.name === s.name) return;
					setInjectedSkills((prev) => {
						const next = prev.map((x) => (x.id === s.id ? { ...x, name: d.name } : x));
						if (sessionId) {
							try {
								localStorage.setItem(injectedSkillsKey(sessionId), JSON.stringify(next));
							} catch {
								/* ignore */
							}
						}
						return next;
					});
				})
				.catch(() => {});
		}
		return () => {
			cancelled = true;
		};
	}, [injectedSkills, sessionId]);

	// Owns the chat area's direct-chat mode. Declared before `useMessages` so its
	// event handler can be wired in below: a direct turn's events arrive on the
	// session's stream like any other, tagged with the instance they belong to.
	const directChat = useDirectChat(sessionId);
	// Extracted as a stable callback: the panel memo below would otherwise have
	// to depend on `directChat`, a fresh object each render, and rebuild every
	// panel on every keystroke.
	const { enter: directEnter } = directChat;
	const enterDirect = useCallback(
		(target: { agent: string; handle: string }) => void directEnter(target),
		[directEnter],
	);
	const { msgs, phase, send, onUserConfirm, onSubagentConfirm, subagentHitl, interrupt } =
		useMessages(agentId, sessionId, {
			onTeamUpdated: handleTeamUpdated,
			onStateUpdated: handleStateUpdated,
			onDagRunStarted: handleDagRunStarted,
			onDagNodeUpdated: handleDagNodeUpdated,
			onDagRunCompleted: handleDagRunCompleted,
			onSubagentInstanceUpdated: handleSubagentInstanceUpdated,
			onSkillsInjected: handleSkillsInjected,
			onSubagentDirect: directChat.onDirectEvent,
		});
	// Skills and MCP servers come from raven's own runtime, not the AgentScope
	// workspace: the chat agent is the gateway, and the workspace stores are not
	// wired to it (they report an empty skill set and MCP servers the agent
	// cannot see).
	const {
		skills: ravenSkills,
		loading: ravenSkillsLoading,
		remove: removeRavenSkill,
	} = useRavenSkills();
	const {
		mcps,
		loading: mcpsLoading,
		loadError: mcpsLoadError,
		connected: mcpsConnected,
		add: addRavenMcps,
		remove: removeRavenMcp,
	} = useRavenMcps();
	// The dialog emits the standard mcpServers JSON shape; raven's config uses
	// its own field names, so adapt at this boundary and warn that MCP tools
	// only register at connect time.
	const addMcps = useCallback(
		async (clients: MCPClient[]) => {
			const res = await addRavenMcps(clients.map(toRavenMcpServer));
			// `applied` is the live half: the write always lands, and this says
			// whether the running agent was reachable to be reconciled to it.
			// `restart_required` used to carry this and is now always false --
			// each server owns its transport, so nothing has to be restarted.
			if (res && res.applied === false) toast.info(t('panel.mcp.savedNotApplied'));
		},
		[addRavenMcps, t],
	);
	// Same warning on the way out: removing a server rewrites the config, but
	// its already-registered tools live until the gateway restarts.
	const removeMcp = useCallback(
		async (name: string) => {
			const res = await removeRavenMcp(name);
			if (res && res.applied === false) toast.info(t('panel.mcp.savedNotAppliedRemove'));
		},
		[removeRavenMcp, t],
	);
	const { knowledgeBases, loading: knowledgeBasesLoading } = useKnowledgeBases();
	const { schema: kbMiddlewareSchema } = useKnowledgeBaseMiddlewareSchema();

	// Toggle a panel open/closed from the top-bar buttons.
	const togglePanel = useCallback((key: PanelKey) => {
		setPanelLayout((layout) =>
			layout.some((column) => column.includes(key))
				? closePanelInLayout(layout, key)
				: openPanelInLayout(layout, key),
		);
	}, []);

	// Close a panel (driven by the panel's own close button).
	const closePanel = useCallback((key: PanelKey) => {
		setPanelLayout((layout) => closePanelInLayout(layout, key));
	}, []);

	const isPanelOpen = useCallback(
		(key: PanelKey) => panelLayout.some((column) => column.includes(key)),
		[panelLayout],
	);

	/**
	 * Persist a knowledge-base attachment change. `null` detaches every
	 * knowledge base from this session, removing the `RAGMiddleware`.
	 *
	 * Declared above `panels` (rather than alongside the other model
	 * handlers below) because `panels` is built inside `useMemo` and
	 * references this handler eagerly — a later `const` would still be
	 * in the temporal dead zone when the memo factory runs on first
	 * render.
	 *
	 * @param config - New attachment, or `null` to detach all.
	 */
	const handleKnowledgeConfigChange = useCallback(
		async (config: SessionKnowledgeConfig | null) => {
			if (!sessionId || !agentId) return;
			setSelectedKnowledgeConfig(config);
			await sessionApi.update(sessionId, agentId, { knowledge_config: config });
			await refetchSessions();
		},
		[sessionId, agentId, refetchSessions],
	);

	// Build the panel descriptors with live data. Rebuilt on every
	// data change so the dock always renders the latest state — the
	// dock itself stays free of any data dependency.
	const panels = useMemo<Record<PanelKey, PanelDescriptor>>(
		() => ({
			plan: {
				title: t('panel.plan.title'),
				icon: <ListTodo className="size-4" />,
				content: <TaskPanel tasksContext={tasksContext} />,
			},
			mcp: {
				title: 'MCP',
				icon: <MCPSvg className="size-4" />,
				content: (
					<McpPanel
						mcps={mcps}
						loading={mcpsLoading}
						loadError={mcpsLoadError}
						connected={mcpsConnected}
						onAdd={addMcps}
						onRemove={removeMcp}
					/>
				),
			},
			skill: {
				title: t('panel.skill.title'),
				icon: <BookText className="size-4" />,
				content: (
					<SkillPanel
						skills={ravenSkills}
						loading={ravenSkillsLoading}
						onRemove={removeRavenSkill}
						injected={injectedSkills}
					/>
				),
			},
			subagent: {
				title: t('subagent-monitor.title'),
				icon: <Bot className="size-4" />,
				content: (
					<SubagentInstanceMonitor
						msgs={msgs}
						onEnterDirect={enterDirect}
						sessionId={sessionId}
						subagents={subagents}
						statefulNames={statefulNames}
						phase={phase}
					/>
				),
			},
			delivery: {
				title: 'Deliverables',
				icon: <Package className="size-4" />,
				content: <DeliverablesPanel msgs={msgs} />,
			},
			permission: {
				title: (
					<span className="flex items-center gap-x-2">
						{t('panel.permission.title')}
						{permissionContext?.mode ? (
							<Badge variant="outline" className="capitalize">
								{t('panel.permission.mode', { mode: permissionContext.mode })}
							</Badge>
						) : null}
					</span>
				),
				icon: <ShieldCheck className="size-4" />,
				content: <PermissionPanel permissionContext={permissionContext} />,
			},
			knowledge: {
				title: (
					<span className="flex items-center gap-x-2">
						{t('panel.knowledge.title')}
						{selectedKnowledgeConfig?.knowledge_base_ids.length ? (
							<Badge variant="outline">
								{selectedKnowledgeConfig.knowledge_base_ids.length}
							</Badge>
						) : null}
					</span>
				),
				icon: <Database className="size-4" />,
				actions: (
					<KnowledgeBaseParametersPopover
						value={selectedKnowledgeConfig}
						schema={kbMiddlewareSchema}
						onChange={handleKnowledgeConfigChange}
						disabled={!sessionId}
					/>
				),
				content: (
					<KnowledgeBasePanel
						knowledgeBases={knowledgeBases}
						loading={knowledgeBasesLoading}
						value={selectedKnowledgeConfig}
						onChange={handleKnowledgeConfigChange}
						disabled={!sessionId}
					/>
				),
			},
		}),
		[
			t,
			enterDirect,
			tasksContext,
			mcps,
			mcpsLoading,
			mcpsLoadError,
			mcpsConnected,
			addMcps,
			removeMcp,
			ravenSkills,
			ravenSkillsLoading,
			removeRavenSkill,
			injectedSkills,
			msgs,
			subagents,
			statefulNames,
			phase,
			permissionContext,
			knowledgeBases,
			knowledgeBasesLoading,
			selectedKnowledgeConfig,
			kbMiddlewareSchema,
			handleKnowledgeConfigChange,
			sessionId,
		],
	);

	const deliverableCount = useMemo(() => deriveDeliverables(msgs).length, [msgs]);

	const subagentNameSet = useMemo(() => new Set(subagents.map((s) => s.name)), [subagents]);

	const dagRunsState = useMemo(
		() => ({
			dagRuns,
			latestRunId: latestDagRunId,
			runIdByToolCallId: buildRunIdByToolCallId(msgs, dagRuns),
			sessionKey: sessionId ? `web:${sessionId}` : undefined,
		}),
		[dagRuns, latestDagRunId, msgs, sessionId],
	);

	const subagentInstancesState = useMemo(
		() => ({ instances: subagentInstances }),
		[subagentInstances],
	);

	const view = sessions.find((v) => v.session.id === sessionId) ?? null;

	// ChatViewport keeps its own `useSessions(agentId)` instance (the
	// outer page has a separate one). Its built-in fetch only fires on
	// `agentId` change, so when the outer page creates a new session
	// under the same agent, this list doesn't auto-refresh. Without
	// this refetch, `view` would stay `null` for the brand-new session
	// id and every effect below would early-return on `!view`,
	// leaving the model select and friends pinned to whatever the
	// previously-viewed session had configured.
	useEffect(() => {
		if (!sessionId) return;
		if (view) return;
		refetchSessions();
	}, [sessionId, view, refetchSessions]);

	// Reset local UI state when the target session changes. Otherwise
	// the model select (and disabled-state guards on `send`) would
	// show the previous session's model during the in-flight window
	// before `view` repopulates — and an immediate send would post to
	// a session whose backend config doesn't actually have that model.
	useEffect(() => {
		setSelectedModel(null);
		setSelectedFallbackModel(null);
		setSelectedTTSModel(null);
		setSelectedKnowledgeConfig(null);
		setSelectedWorkDir(null);
		setWorkDirError(null);
		setDagRuns({});
		setLatestDagRunId(null);
		setDagRunRestorePending(false);
		setSubagentInstances({});
		// Rehydrate this conversation's injected-skills list from localStorage
		// (survives refresh; other sessions' lists stay isolated by key).
		setInjectedSkills(readInjectedSkills(sessionId));
	}, [sessionId]);

	// Rebuild this session's DAG graphs from their durable run dirs. The dag_*
	// CustomEvents that normally fill `dagRuns` are live-only — nothing replays
	// them, and a gateway-mode tool result carries no manifest metadata — so
	// without this a reload redraws every graph as all-pending. Best-effort:
	// both calls are `silent`, and a run whose dir was pruned is simply skipped.
	//
	// `dagRestoreTick` re-runs it while a restored run is still unfinalized, so
	// a page reloaded *during* a run keeps advancing instead of freezing at
	// whatever the first read saw. The merge is monotonic (see
	// `mergeRestoredRun`), so a poll racing a live event cannot undo it, and an
	// unchanged poll returns the map by identity and re-renders nothing.
	useEffect(() => {
		if (!sessionId) return;
		let cancelled = false;
		const sessionKey = `web:${sessionId}`;
		(async () => {
			try {
				const { instances } = await ravenConfigApi.listSubagentInstances(sessionKey);
				const runIds = dagRunIdsOf(instances ?? []);
				if (cancelled) return;
				if (runIds.length === 0) {
					setDagRunRestorePending(false);
					return;
				}
				const runs = (
					await Promise.all(
						runIds.map((id) =>
							ravenConfigApi
								.getDagRun(id, sessionKey)
								.then((r) => r.run)
								.catch(() => null),
						),
					)
				).filter((r) => r !== null);
				if (cancelled) return;
				setDagRuns((prev) => mergeRestoredRuns(prev, runs));
				setLatestDagRunId((prev) => prev ?? runIds[0] ?? null);
				setDagRunRestorePending(runs.some((r) => !r.finalized));
			} catch {
				// A restore is decoration on top of the live stream; failing it
				// must never take the conversation down with it.
			}
		})();
		return () => {
			cancelled = true;
		};
	}, [sessionId, dagRestoreTick]);

	useEffect(() => {
		if (!dagRunRestorePending) return;
		const id = setInterval(() => setDagRestoreTick((t) => t + 1), DAG_RESTORE_POLL_MS);
		return () => clearInterval(id);
	}, [dagRunRestorePending]);

	// Sync tasksContext from the session snapshot. Real-time updates
	// arrive via the CustomEvent(name="state_updated") → the
	// onStateUpdated callback above. We always mirror the snapshot
	// (including clearing to null when the session is gone or has no
	// tasks yet) so that switching sessions doesn't leak stale tasks
	// from the previous one.
	useEffect(() => {
		if (!view) {
			setTasksContext(null);
			return;
		}
		const tc = (view.session.state as Record<string, unknown>)?.tasks_context as
			| TaskContext
			| undefined;
		setTasksContext(tc ?? null);
	}, [view]);

	// Sync permissionContext from the session snapshot, mirroring the
	// tasksContext approach above. Real-time updates arrive via the
	// state_updated event → handleStateUpdated. Clearing to null when
	// the session is gone avoids leaking stale rules across sessions.
	useEffect(() => {
		if (!view) {
			setPermissionContext(null);
			return;
		}
		const pc = (view.session.state as Record<string, unknown>)?.permission_context as
			| PermissionContext
			| undefined;
		setPermissionContext(pc ?? null);
	}, [view]);

	// Sync selectedFallbackModel/TTS/knowledge from the session record, and
	// keep `chat_model_config` non-null. AgentScope rejects a turn whose
	// session has no `chat_model_config` before the gateway-mode `get_model`
	// shim ever runs, so a session that has none gets the inert placeholder —
	// the model that actually routes a turn is the session's raven override,
	// restored below independently of this. `work_dir` gets the same
	// treatment: it is only AgentScope's display copy, so it is restored from
	// raven below rather than read here.
	//
	// Important: skip while `view` is still loading. Otherwise the
	// in-flight window between "agentId changed" and "useSessions
	// returned the new list" looks like "session has no model" and
	// we would racily write the placeholder, clobbering an in-flight update.
	useEffect(() => {
		if (!view) return;

		if (!view.session.config.chat_model_config && sessionId && agentId) {
			sessionApi
				.update(sessionId, agentId, { chat_model_config: AGENTSCOPE_MODEL_PLACEHOLDER })
				.then(() => refetchSessions())
				.catch((err) => {
					// The composer is live as soon as `sessionId` exists, and a turn
					// on a session with no chat_model_config 404s inside
					// ChatService.run, which logs and returns nothing. So a swallowed
					// failure here leaves a session that accepts messages forever
					// and answers none -- say so on top of the client's raw toast.
					console.error('failed to seed chat_model_config', err);
					toast.error(t('chat.modelPlaceholderFailed'));
				});
		}

		setSelectedFallbackModel(view.session.config.fallback_chat_model_config ?? null);
		setSelectedTTSModel(view.session.config.tts_model_config ?? null);
		setSelectedKnowledgeConfig(view.session.config.knowledge_config ?? null);
	}, [view, sessionId, agentId, t]);

	// Restore selectedModel from raven's per-session override — the value
	// that actually routes a turn — rather than from `chat_model_config`,
	// which is the inert placeholder until the user picks a model and only a
	// display copy of the pick after that. `cancelled` guards against a slow
	// response for a session the user has already switched away from
	// overwriting the newly selected session's model.
	useEffect(() => {
		if (!view || !sessionId) return;
		let cancelled = false;
		ravenSessionModelApi
			.get(`web:${sessionId}`)
			.then(({ model, default: fallback }) => {
				if (cancelled) return;
				setDefaultModel(fallback);
				setSelectedModel(
					model ? { type: '', credential_id: 'raven', model, parameters: {} } : null,
				);
			})
			.catch(() => {});
		return () => {
			cancelled = true;
		};
	}, [sessionId, view]);

	// Restore selectedWorkDir from raven's per-session override, same
	// reasoning as the model restore above: `work_dir` on the session record
	// is only AgentScope's display copy, the value that binds the session's
	// next turn is raven's own override.
	useEffect(() => {
		if (!view || !sessionId) return;
		let cancelled = false;
		ravenSessionWorkdirApi
			.get(`web:${sessionId}`)
			.then(({ workdir }) => {
				if (cancelled) return;
				setSelectedWorkDir(workdir);
			})
			.catch(() => {});
		return () => {
			cancelled = true;
		};
	}, [sessionId, view]);

	// Sync selectedPermissionMode when the session changes. Same
	// loading-window guard as above — don't reset the displayed mode
	// to "default" while the new session view is still on the wire.
	useEffect(() => {
		if (!view) return;
		const mode = (view.session.state?.permission_context as Record<string, unknown>)
			?.mode as string;
		setSelectedPermissionMode(mode ?? 'default');
	}, [sessionId, view]);

	/**
	 * Persist a model change. The raven write is the one that actually
	 * routes the next turn, so it goes first and its rejection (e.g. a
	 * model no configured provider can serve) propagates -- surfaced by the
	 * API client's automatic error toast -- instead of being masked by a
	 * chat_model_config update that would otherwise "succeed". The local
	 * selection only updates once that write succeeds, and from its
	 * response's `model` (the backend `.strip()`s the value it stores, so
	 * the response is authoritative, not the request); a rejected write
	 * leaves the previous selection displayed instead of showing a switch
	 * that never took effect.
	 *
	 * LlmSelect fires this from a synchronous `onSelect`, so nothing awaits the
	 * returned promise: a rejection has to be caught here or it becomes an
	 * unhandled rejection. The API client has already toasted the reason.
	 *
	 * @param config - New chat model config; `null` is ignored
	 *   because the primary selector does not allow clearing.
	 */
	const handleLlmChange = async (config: ChatModelConfig | null) => {
		if (!config || !sessionId || !agentId) return;
		try {
			const { model } = await ravenSessionModelApi.set(`web:${sessionId}`, config.model);
			setSelectedModel({ ...config, model: model ?? config.model });
			await sessionApi.update(sessionId, agentId, { chat_model_config: config });
			await refetchSessions();
		} catch (err) {
			console.error('failed to set the session model', err);
		}
	};

	/**
	 * Persist a parameter change on the currently selected model.
	 *
	 * @param parameters - New parameter map (model-provider specific).
	 */
	const handleParametersChange = async (parameters: Record<string, unknown>) => {
		if (!selectedModel || !sessionId || !agentId) return;
		const updated = { ...selectedModel, parameters };
		setSelectedModel(updated);
		await sessionApi.update(sessionId, agentId, { chat_model_config: updated });
		await refetchSessions();
	};

	/**
	 * Persist a fallback-model change. `null` clears the fallback.
	 *
	 * @param config - New fallback config or `null` to clear.
	 */
	const handleFallbackChange = async (config: ChatModelConfig | null) => {
		if (!sessionId || !agentId) return;
		setSelectedFallbackModel(config);
		await sessionApi.update(sessionId, agentId, { fallback_chat_model_config: config });
		await refetchSessions();
	};

	/**
	 * Persist a TTS model change. `null` disables TTS.
	 *
	 * @param config - New TTS config or `null` to disable.
	 */
	const handleTTSChange = async (config: TTSModelConfig | null) => {
		if (!sessionId || !agentId) return;
		setSelectedTTSModel(config);
		await sessionApi.update(sessionId, agentId, { tts_model_config: config });
		await refetchSessions();
	};

	/**
	 * Persist a permission-mode change.
	 *
	 * @param mode - New permission mode (e.g. `default`, `explore`).
	 */
	const handlePermissionModeChange = async (mode: string) => {
		setSelectedPermissionMode(mode);
		if (!sessionId || !agentId) return;
		await sessionApi.update(sessionId, agentId, { permission_mode: mode });
		await refetchSessions();
	};

	/**
	 * Persist a working-directory override. `null` resets to the default
	 * session workspace directory. The raven write is the one that actually
	 * binds the session's next turn, so it goes first and its rejection --
	 * a relative path, a path inside the agent's protected trees, or a
	 * session with work still in flight -- is the only failure reported as
	 * `workDirError` ("the override did not take"). Takes effect on the next
	 * turn only; it does not move a turn already running.
	 *
	 * The AgentScope `work_dir` copy and the session refetch that follow are
	 * a display-sync step, not the change itself: the raven write already
	 * succeeded and `selectedWorkDir` already reflects it by the time they
	 * run, so their failure is reported as a toast (via the API client's
	 * default error handling) rather than `workDirError` -- conflating the
	 * two would tell the user their override didn't take when it did.
	 *
	 * @param path - New absolute working directory, or `null` to reset.
	 */
	const handleWorkDirChange = async (path: string | null) => {
		if (!sessionId || !agentId) return;
		let workdir: string | null;
		try {
			({ workdir } = await ravenSessionWorkdirApi.set(`web:${sessionId}`, path));
		} catch (err) {
			setWorkDirError(formatApiErrorForAlert(err));
			return;
		}
		setWorkDirError(null);
		setSelectedWorkDir(workdir);
		try {
			await sessionApi.update(sessionId, agentId, { work_dir: workdir });
			await refetchSessions();
		} catch (err) {
			console.error('failed to sync the work_dir display copy', err);
		}
	};

	return (
		<SubagentNamesContext.Provider value={subagentNameSet}>
			<SubagentInstancesContext.Provider value={subagentInstancesState}>
				<DagRunsContext.Provider value={dagRunsState}>
					<main className="flex size-full">
						<ResizablePanelGroup orientation="horizontal">
							<ResizablePanel className="flex flex-1" minSize="24rem">
								<div className="flex flex-col flex-1 min-h-0 min-w-0 overflow-x-hidden p-2">
									<div className="flex flex-row gap-x-2 justify-between">
										<div
											id="tour-llm-select"
											className="flex flex-row items-center gap-x-1"
										>
											<SidebarTrigger className="md:hidden" />
											<LlmSelect
												value={selectedModel}
												onChange={handleLlmChange}
												ravenGroups={ravenGroups}
												ravenLoading={ravenLoading}
												placeholder={defaultModel ?? undefined}
											/>
											<ModelParametersPopover
												selectedModel={selectedModel}
												modelCard={null}
												onChange={handleParametersChange}
												selectedFallbackModel={selectedFallbackModel}
												onFallbackChange={handleFallbackChange}
												selectedTTSModel={selectedTTSModel}
												onTTSChange={handleTTSChange}
											/>
										</div>
										<div
											id="tour-permission-mode"
											className="flex flex-row gap-x-1"
										>
											{/*
											 * Hard-disabled until raven grows a tool-call
											 * confirmation mechanism. Writing the mode still
											 * works (it lands in AgentScope's
											 * session.state.permission_context), but nothing
											 * enforces it: the PermissionEngine lives on
											 * AgentScope's own agent, and gateway mode swaps
											 * that out for the duck-typed RavenGatewayAgent.
											 * Re-enable by restoring `disabled={!sessionId}`.
											 */}
											<PermissionModeSelect
												value={selectedPermissionMode}
												disabled
												disabledReason={t('permission-mode.unsupported')}
												onChange={handlePermissionModeChange}
											/>
											<DropdownMenu>
												<DropdownMenuTrigger asChild>
													<Button
														variant="ghost"
														size="sm"
														className="gap-1 px-2"
													>
														<PanelRight />
														<ChevronDown className="size-3 text-muted-foreground" />
													</Button>
												</DropdownMenuTrigger>
												<DropdownMenuContent align="end" className="w-auto">
													<DropdownMenuCheckboxItem
														checked={isPanelOpen('plan')}
														onCheckedChange={() => togglePanel('plan')}
														onSelect={(e) => e.preventDefault()}
													>
														<ListTodo />
														{t('panel.plan.title')}
													</DropdownMenuCheckboxItem>
													<DropdownMenuCheckboxItem
														checked={isPanelOpen('mcp')}
														onCheckedChange={() => togglePanel('mcp')}
														onSelect={(e) => e.preventDefault()}
													>
														<MCPSvg className="size-4" />
														MCP
													</DropdownMenuCheckboxItem>
													<DropdownMenuCheckboxItem
														checked={isPanelOpen('skill')}
														onCheckedChange={() => togglePanel('skill')}
														onSelect={(e) => e.preventDefault()}
													>
														<BookText />
														{t('panel.skill.title')}
													</DropdownMenuCheckboxItem>
													<DropdownMenuCheckboxItem
														checked={isPanelOpen('subagent')}
														onCheckedChange={() =>
															togglePanel('subagent')
														}
														onSelect={(e) => e.preventDefault()}
													>
														<Bot />
														{t('subagent-monitor.title')}
													</DropdownMenuCheckboxItem>
													<DropdownMenuCheckboxItem
														checked={isPanelOpen('delivery')}
														onCheckedChange={() =>
															togglePanel('delivery')
														}
														onSelect={(e) => e.preventDefault()}
													>
														<Package />
														Deliverables
														{deliverableCount > 0 && (
															<Badge
																variant="outline"
																className="ml-auto"
															>
																{deliverableCount}
															</Badge>
														)}
													</DropdownMenuCheckboxItem>
													<DropdownMenuCheckboxItem
														checked={isPanelOpen('permission')}
														onCheckedChange={() =>
															togglePanel('permission')
														}
														onSelect={(e) => e.preventDefault()}
													>
														<ShieldCheck />
														{t('panel.permission.title')}
													</DropdownMenuCheckboxItem>
													<DropdownMenuCheckboxItem
														checked={isPanelOpen('knowledge')}
														onCheckedChange={() =>
															togglePanel('knowledge')
														}
														onSelect={(e) => e.preventDefault()}
													>
														<Database />
														{t('panel.knowledge.title')}
													</DropdownMenuCheckboxItem>
												</DropdownMenuContent>
											</DropdownMenu>
										</div>
									</div>
									{directChat.active && (
										// Without this a user who enters a direct chat has no way
										// back and no sign of whose conversation they are in: the
										// chat area looks exactly as it always does, only with
										// different content.
										<div className="flex justify-center px-2 pb-1">
											<div className="flex max-w-[var(--chat-content-w)] w-full items-center gap-2 text-xs">
												<button
													type="button"
													className="rounded-sm border px-2 py-0.5 hover:bg-accent"
													onClick={directChat.leave}
												>
													{'\u2039 Raven'}
												</button>
												<span className="truncate text-muted-foreground">
													{directChat.active.agent}/
													{directChat.active.handle}
												</span>
											</div>
										</div>
									)}
									<div className="flex flex-1 justify-center min-h-0 overflow-hidden relative [--chat-content-w:48rem]">
										<ChatContent
											className={'max-w-[var(--chat-content-w)] w-full'}
											msgs={
												directChat.active
													? directChat.viewMsgs
													: msgs
											}
											phase={phase}
											disabled={
												!sessionId ||
												directChat.pausedReason !== null
											}
											onSend={
												directChat.active
													? (content) =>
															void directChat.send(
																contentText(content),
															)
													: send
											}
											onUserConfirm={onUserConfirm}
											onInterrupt={interrupt}
											footerSlot={
												subagentHitl.length > 0 ? (
													<div className="space-y-2 pb-2">
														{subagentHitl.map((entry) => (
															<SubagentHitlCard
																key={`${entry.worker_session_id}:${entry.reply_id}`}
																entry={entry}
																onConfirm={(
																	toolCall,
																	confirm,
																	rules,
																) =>
																	onSubagentConfirm(
																		entry,
																		toolCall,
																		confirm,
																		rules,
																	)
																}
															/>
														))}
													</div>
												) : null
											}
											belowInputSlot={
												sessionId ? (
													<WorkingDirectoryControl
														value={selectedWorkDir}
														disabled={!sessionId}
														onChange={handleWorkDirChange}
														error={workDirError}
													/>
												) : null
											}
											fileProcessor={async (file) => {
												const filePath = (file as File & { path?: string })
													.path;
												if (filePath) {
													return {
														id: crypto.randomUUID(),
														type: 'data' as const,
														source: {
															type: 'url' as const,
															url: `file://${filePath}`,
															media_type:
																file.type ||
																'application/octet-stream',
														},
														name: file.name,
													};
												}
												if (file.type === 'text/plain') {
													const text = await file.text();
													return {
														id: crypto.randomUUID(),
														type: 'text' as const,
														text: `[File: ${file.name}]\n${text}`,
													};
												}
												const buffer = await file.arrayBuffer();
												const bytes = new Uint8Array(buffer);
												let binary = '';
												for (let i = 0; i < bytes.byteLength; i++) {
													binary += String.fromCharCode(bytes[i]);
												}
												const base64 = btoa(binary);
												return {
													id: crypto.randomUUID(),
													type: 'data' as const,
													source: {
														type: 'base64' as const,
														media_type:
															file.type || 'application/octet-stream',
														data: base64,
													},
													name: file.name,
												};
											}}
										/>
									</div>
								</div>
							</ResizablePanel>
							{panelLayout.length > 0 && (
								<ResizableHandle withHandle className="bg-transparent" />
							)}
							<PanelDock
								layout={panelLayout}
								panels={panels}
								onClosePanel={closePanel}
							/>
						</ResizablePanelGroup>
					</main>
				</DagRunsContext.Provider>
			</SubagentInstancesContext.Provider>
		</SubagentNamesContext.Provider>
	);
}
