import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { ChatViewport } from './ChatViewport';
import type { SessionRecord } from '@/api';
import { ChatGroupDialog } from '@/components/dialog/ChatGroupDialog';
import { DeleteDialog } from '@/components/dialog/DeleteDialog';
import { RenameDialog } from '@/components/dialog/RenameDialog';
import { AgentSelect } from '@/components/select/AgentSelect';
import { TeamSidebar } from '@/components/team/TeamSidebar';
import { ChatTourController } from '@/components/tour/ChatTourController';
import { Button } from '@/components/ui/button';
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuItem,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
	Empty,
	EmptyHeader,
	EmptyTitle,
	EmptyDescription,
	EmptyContent,
	EmptyMedia,
} from '@/components/ui/empty';
import {
	Sidebar,
	SidebarContent,
	SidebarFooter,
	SidebarGroup,
	SidebarGroupAction,
	SidebarGroupContent,
	SidebarGroupLabel,
	SidebarHeader,
	SidebarMenu,
	SidebarMenuAction,
	SidebarMenuButton,
	SidebarMenuItem,
	SidebarProvider,
	useSidebar,
} from '@/components/ui/sidebar';
import { AudioProvider } from '@/context/AudioContext';
import { useAgents } from '@/hooks/useAgents';
import { useRavenSubagents } from '@/hooks/useRavenSubagents';
import { useSessions } from '@/hooks/useSessions';
import { useTranslation } from '@/i18n/useI18n.ts';
import Plus from '~icons/solar/add-square-linear';
import CalendarClock from '~icons/solar/alarm-bold-duotone';
import MessageSquareDashed from '~icons/solar/chat-square-like-bold-duotone';
import Ellipsis from '~icons/solar/menu-dots-linear';
import Pencil from '~icons/solar/pen-2-bold-duotone';
import BotMessageSquare from '~icons/solar/programming-bold-duotone';
import Trash2 from '~icons/solar/trash-bin-minimalistic-bold-duotone';

/** Where the last-opened chat group id is stashed for a bare `/chat` landing. */
const LAST_GROUP_KEY = 'chat_last_group';

/**
 * The chat page's outer shell. Responsibilities split cleanly:
 *
 * - **This component** owns *which* `(agent, session)` is being
 *   viewed. The URL is the single source of truth: every selection
 *   (agent dropdown, session row, team member, new session) is a
 *   ``navigate(...)`` call. State is derived from ``useParams``,
 *   never duplicated in React state. Renders the main left sidebar
 *   (agent picker + session list + create/rename/delete actions) and
 *   computes the ``effective`` ids to feed the chat viewport.
 * - **`ChatViewport`** owns *what* to render for that pair: messages,
 *   model selector, permission mode, workspace drawer, team sidebar.
 *
 * Splitting along this seam means switching between the leader's
 * session and a focused team member is just a prop change for the
 * viewport — the leader's session list stays anchored in this outer
 * sidebar. Driving everything off URL also gets us browser back /
 * forward, shareable links, and refresh-preserving state for free.
 *
 * @returns The chat page JSX.
 */
const ChatPageInner = () => {
	const navigate = useNavigate();
	const {
		agentId: urlAgentId,
		sessionId: urlSessionId,
		memberId: urlMemberId,
	} = useParams<{
		agentId?: string;
		sessionId?: string;
		memberId?: string;
	}>();
	const { t } = useTranslation();
	const {
		agents,
		refetch: refetchAgents,
		update: updateAgent,
		remove: removeAgent,
	} = useAgents();
	const {
		sessions,
		refetch: refetchSessions,
		create: createSession,
		update: updateSession,
		remove: removeSession,
	} = useSessions(urlAgentId ?? null);

	const { isMobile, setOpen, setOpenMobile } = useSidebar();
	const [renameGroupOpen, setRenameGroupOpen] = useState(false);
	const [deleteOpen, setDeleteOpen] = useState(false);
	const [renameOpen, setRenameOpen] = useState(false);
	const [renameSession, setRenameSession] = useState<SessionRecord | null>(null);
	const [deleteSessionOpen, setDeleteSessionOpen] = useState(false);
	const [sessionToDelete, setSessionToDelete] = useState<SessionRecord | null>(null);
	const { agents: subagents, statefulNames } = useRavenSubagents();

	const selectedAgent = agents.find((a) => a.id === urlAgentId) ?? null;
	const currentView = sessions.find((v) => v.session.id === urlSessionId) ?? null;
	const hasScheduleSessions = sessions.some((v) => v.session.source === 'schedule');

	// "Inner focus" — when the URL carries a third `:memberId` segment
	// the user is drilling into a team member's chat. The main sidebar
	// stays anchored on the outer (leader) session; only the chat
	// viewport follows this inner focus. When `urlMemberId` is
	// undefined or doesn't resolve to a known team member, the inner
	// focus collapses back to the outer (leader) session.
	const focusedMember = urlMemberId
		? (currentView?.team?.members.find((m) => m.agent.id === urlMemberId) ?? null)
		: null;
	const effectiveAgentId =
		focusedMember && focusedMember.session_id ? focusedMember.agent.id : (urlAgentId ?? null);
	const effectiveSessionId =
		focusedMember && focusedMember.session_id
			? focusedMember.session_id
			: (urlSessionId ?? null);

	// Remember the open chat group so a later bare `/chat` — the rail's Chat
	// icon, a bookmark, a fresh tab — reopens it. A full-URL refresh does not
	// need this (the group is already in the path); landing on `/chat` does.
	useEffect(() => {
		if (urlAgentId) localStorage.setItem(LAST_GROUP_KEY, urlAgentId);
	}, [urlAgentId]);

	// Redirect: URL is missing a group → restore the remembered one and rewrite
	// the URL in-place (replace so we don't pollute history). The remembered id
	// is checked against the live list first: a group deleted since it was
	// stored would otherwise redirect to a 'group' with no sessions.
	useEffect(() => {
		if (urlAgentId || agents.length === 0) return;
		const remembered = localStorage.getItem(LAST_GROUP_KEY);
		const target = agents.find((a) => a.id === remembered)?.id ?? agents[0].id;
		navigate(`/chat/${target}`, { replace: true });
	}, [agents, urlAgentId, navigate]);

	// Redirect: URL has an agent but no session, or its sessionId no
	// longer exists for this agent → pick the first available session.
	useEffect(() => {
		if (!urlAgentId || sessions.length === 0) return;
		const matches = urlSessionId && sessions.some((v) => v.session.id === urlSessionId);
		if (matches) return;
		navigate(`/chat/${urlAgentId}/${sessions[0].session.id}`, { replace: true });
	}, [urlAgentId, urlSessionId, sessions, navigate]);

	/**
	 * Create a new session under the currently selected agent, seeding
	 * AgentScope's `chat_model_config` and `fallback_chat_model_config` from the
	 * currently open session — or, when there is none, from any other session
	 * under this agent. Navigates to the freshly created session.
	 *
	 * The seeded `chat_model_config` is NOT the model a turn runs on: that is
	 * raven's `Session.metadata["model"]`, written only through
	 * `raven.session.model.set` and not copied here. So a new chat starts with
	 * no per-session override and runs on `agents.defaults.model` however the
	 * previous session was configured; only the fallback model, still
	 * AgentScope's, is genuinely inherited.
	 */
	const handleCreateSession = async () => {
		if (!urlAgentId) return;
		const seedConfig = currentView?.session.config ?? sessions[0]?.session.config;
		const res = await createSession({
			agent_id: urlAgentId,
			...(seedConfig?.chat_model_config
				? { chat_model_config: seedConfig.chat_model_config }
				: {}),
			...(seedConfig?.fallback_chat_model_config
				? { fallback_chat_model_config: seedConfig.fallback_chat_model_config }
				: {}),
		});
		navigate(`/chat/${urlAgentId}/${res.session_id}`);
	};

	/**
	 * Switch to a freshly created chat group. The list is refreshed before the
	 * navigation so the group picker and its rename/delete actions resolve the
	 * new id on the same render the URL starts pointing at it; the
	 * session-redirect effect then lands on that group's first session.
	 *
	 * @param groupId - Id of the chat group that was just created.
	 */
	const handleGroupCreated = async (groupId: string) => {
		await refetchAgents();
		navigate(`/chat/${groupId}`);
	};

	const handleAgentDeleted = async () => {
		navigate('/chat', { replace: true });
		await refetchAgents();
	};

	const handleDeleteSession = async (sessionId: string) => {
		await removeSession(sessionId);
		// If we just removed the session the URL is pointing at, fall
		// back to the parent /chat/:agentId path; the redirect effect
		// will then pick the next available session.
		if (sessionId === urlSessionId && urlAgentId) {
			navigate(`/chat/${urlAgentId}`, { replace: true });
		}
	};

	const requestDeleteSession = (session: SessionRecord) => {
		setSessionToDelete(session);
		setDeleteSessionOpen(true);
	};

	const handleRenameConfirm = async (name: string) => {
		if (!renameSession) return;
		await updateSession(renameSession.id, { name });
	};

	const handleRenameGroupConfirm = async (name: string) => {
		if (!urlAgentId) return;
		await updateAgent(urlAgentId, { name });
		await refetchAgents();
	};

	return (
		<div className="flex h-full w-full">
			{/*
			 * Desktop stays `collapsible="none"` so the session list sits in
			 * normal flow beside the app rail (AppSidebar). Mobile switches to
			 * `offcanvas`, which makes shadcn's Sidebar render its Sheet overlay
			 * (the drawer we want) — instead of the desktop `fixed left-0`
			 * container, which would otherwise cover the app rail.
			 */}
			<Sidebar collapsible={isMobile ? 'offcanvas' : 'none'} className="border-r">
				<SidebarHeader>
					<div className="flex flex-col gap-y-2">
						<div className="flex flex-row gap-x-2 items-center">
							<AgentSelect
								agents={agents}
								value={urlAgentId ?? null}
								onChange={(id) => navigate(`/chat/${id}`)}
							/>
							<Button
								size="icon"
								variant="ghost"
								disabled={!urlAgentId}
								tooltip={t('dialog-group-rename.title')}
								onClick={() => setRenameGroupOpen(true)}
							>
								<Pencil />
							</Button>
							<Button
								size="icon"
								variant="ghost"
								disabled={!urlAgentId}
								onClick={() => setDeleteOpen(true)}
							>
								<Trash2 className="text-destructive" />
							</Button>
						</div>
						<ChatGroupDialog
							onCreated={handleGroupCreated}
							triggerId="tour-create-group"
						/>
					</div>
				</SidebarHeader>
				<SidebarContent className="my-5">
					<SidebarGroup>
						<SidebarGroupLabel>{t('chat.session.label')}</SidebarGroupLabel>
						<SidebarGroupAction asChild>
							<div>
								<Button
									id="tour-create-session"
									size="icon-xs"
									variant="default"
									disabled={!urlAgentId}
									onClick={handleCreateSession}
								>
									<Plus />
								</Button>
							</div>
						</SidebarGroupAction>
						<SidebarGroupContent>
							{sessions.length === 0 ? (
								<Empty className="border-none py-4 min-h-50">
									<EmptyHeader>
										<EmptyMedia variant="icon">
											<MessageSquareDashed />
										</EmptyMedia>
										<EmptyTitle>{t('chat.session.emptyTitle')}</EmptyTitle>
										<EmptyDescription>
											{urlAgentId
												? t('chat.session.emptyHasGroup')
												: t('chat.session.emptyNoGroup')}
										</EmptyDescription>
									</EmptyHeader>
									<EmptyContent>
										<Button
											variant="outline"
											size="sm"
											disabled={!urlAgentId}
											onClick={handleCreateSession}
										>
											{t('chat.session.create')}
										</Button>
									</EmptyContent>
								</Empty>
							) : (
								<SidebarMenu>
									{sessions.map((view) => {
										const session = view.session;
										return (
											<SidebarMenuItem key={session.id}>
												<SidebarMenuButton
													isActive={urlSessionId === session.id}
													onClick={() => {
														navigate(
															`/chat/${urlAgentId}/${session.id}`,
														);
														setOpenMobile(false);
													}}
												>
													{hasScheduleSessions &&
														(session.source === 'schedule' ? (
															<CalendarClock />
														) : (
															<BotMessageSquare />
														))}
													<span className="truncate">
														{session.config.name || session.id}
													</span>
												</SidebarMenuButton>
												<SidebarMenuAction showOnHover>
													<DropdownMenu>
														<DropdownMenuTrigger asChild>
															<Ellipsis />
														</DropdownMenuTrigger>
														<DropdownMenuContent
															side="right"
															align="start"
														>
															<DropdownMenuItem
																onClick={() => {
																	setRenameSession(session);
																	setRenameOpen(true);
																}}
															>
																<Pencil />
																{t('session-menu.rename')}
															</DropdownMenuItem>
															<DropdownMenuItem
																variant="destructive"
																onClick={() =>
																	requestDeleteSession(session)
																}
															>
																<Trash2 />
																{t('session-menu.delete')}
															</DropdownMenuItem>
														</DropdownMenuContent>
													</DropdownMenu>
												</SidebarMenuAction>
											</SidebarMenuItem>
										);
									})}
								</SidebarMenu>
							)}
						</SidebarGroupContent>
					</SidebarGroup>
				</SidebarContent>
				<SidebarFooter />
			</Sidebar>
			{/*
			 * Team sidebar lives at the outer page level (not inside
			 * ChatViewport) so navigating between leader and member
			 * sessions does NOT unmount it. The team data comes from
			 * the leader's session view, which is stable across that
			 * navigation; only `currentSessionId` changes to drive
			 * row highlighting.
			 */}
			{currentView?.team && effectiveSessionId && (
				<TeamSidebar team={currentView.team} currentSessionId={effectiveSessionId} />
			)}
			<div className="flex flex-1 min-w-0">
				<ChatViewport
					agentId={effectiveAgentId}
					sessionId={effectiveSessionId}
					subagents={subagents}
					statefulNames={statefulNames}
					onTeamUpdated={refetchSessions}
				/>
			</div>
			{selectedAgent && (
				<>
					<RenameDialog
						open={renameGroupOpen}
						onOpenChange={setRenameGroupOpen}
						currentName={selectedAgent.data.name}
						onConfirm={handleRenameGroupConfirm}
						title={t('dialog-group-rename.title')}
						description={t('dialog-group-rename.description')}
						label={t('dialog-group-rename.label')}
						placeholder={t('dialog-group-rename.placeholder')}
					/>
					<DeleteDialog
						open={deleteOpen}
						onOpenChange={setDeleteOpen}
						title={t('common.deleteTitle', {
							entity: t('dialog-group-delete.entity'),
							name: selectedAgent.data.name,
						})}
						description={t('common.deleteDescription')}
						confirmLabel={t('dialog-group-delete.confirm')}
						onConfirm={async () => {
							await removeAgent(selectedAgent.id);
							await handleAgentDeleted();
						}}
					/>
				</>
			)}
			<RenameDialog
				open={renameOpen}
				onOpenChange={setRenameOpen}
				currentName={renameSession?.config.name ?? renameSession?.id ?? ''}
				onConfirm={handleRenameConfirm}
				title={t('dialog-session-rename.title')}
				description={t('dialog-session-rename.description')}
				label={t('dialog-session-rename.label')}
				placeholder={t('dialog-session-rename.placeholder')}
			/>
			<DeleteDialog
				open={deleteSessionOpen}
				onOpenChange={setDeleteSessionOpen}
				title={t('common.deleteTitle', {
					entity: t('dialog-session-delete.entity'),
					name: sessionToDelete?.config.name || sessionToDelete?.id || '',
				})}
				description={t('common.deleteDescription')}
				confirmLabel={t('dialog-session-delete.confirm')}
				onConfirm={async () => {
					if (sessionToDelete) {
						await handleDeleteSession(sessionToDelete.id);
					}
				}}
			/>
			<ChatTourController
				agentsCount={agents.length}
				sessionsCount={sessions.length}
				onEnsureSidebarOpen={() => {
					setOpen(true);
					setOpenMobile(true);
				}}
			/>
		</div>
	);
};

export const ChatPage = () => (
	<AudioProvider>
		<SidebarProvider defaultOpen>
			<ChatPageInner />
		</SidebarProvider>
	</AudioProvider>
);
