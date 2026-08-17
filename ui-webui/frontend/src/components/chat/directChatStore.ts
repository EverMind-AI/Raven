/**
 * Direct-chat view state: which instance the chat area is showing, what each
 * instance has said, and which ones are mid-reply.
 *
 * A plain store rather than React state because the rules here are the same
 * ones `ui-tui/src/app/directChatStore.ts` encodes, and they are worth stating
 * once, in a form a test can drive without a renderer. The React layer is a
 * subscription on top.
 *
 * Direct-chat messages deliberately never enter the AgentScope session message
 * list: that list is the main agent's conversation, and keeping these exchanges
 * out of it is the whole point of the feature. What survives a reload is the
 * record on disk, re-read through `subagents.instance.history`.
 */

export interface DirectTargetRef {
	agent: string;
	handle: string;
}

export interface DirectMsg {
	role: 'user' | 'assistant' | 'system';
	text: string;
}

/** The main agent's own view, which is not an instance. */
export const MAIN_VIEW_KEY = 'main';

/**
 * The transcript key for one instance.
 *
 * Length-prefixed on the agent name: an agent name and a handle are both
 * free-form, so a bare join lets `a/b` + `c` collide with `a` + `b/c` and two
 * instances would share one conversation. The service keys its event queues the
 * same way for the same reason.
 */
export function directKey(agent: string, handle: string): string {
	return `${agent.length}:${agent}/${handle}`;
}

/** The view key for a target, or `main` for the main agent. */
export function viewKeyOf(target: DirectTargetRef | null): string {
	return target === null ? MAIN_VIEW_KEY : directKey(target.agent, target.handle);
}

/**
 * Whether two targets name the same instance.
 *
 * Two nulls are deliberately *not* equal: they are both "the main agent", and a
 * null-tolerant compare would route every untagged event into whichever
 * instance happened to be active.
 */
export function isDirectTarget(a: DirectTargetRef | null, b: DirectTargetRef | null): boolean {
	return a !== null && b !== null && a.agent === b.agent && a.handle === b.handle;
}

export interface DirectChatState {
	/** `null` = the main agent. */
	active: DirectTargetRef | null;
	/** view key -> that instance's transcript. */
	transcripts: Record<string, DirectMsg[]>;
	/** View keys with a turn in flight. */
	running: string[];
}

export const emptyDirectChat = (): DirectChatState => ({
	active: null,
	transcripts: {},
	running: [],
});

/** Append a delta, merging it into the trailing message when the role matches. */
export function appendDelta(
	state: DirectChatState,
	key: string,
	role: DirectMsg['role'],
	text: string,
): DirectChatState {
	const prev = state.transcripts[key] ?? [];
	const last = prev[prev.length - 1];
	const next =
		last && last.role === role
			? [...prev.slice(0, -1), { role, text: last.text + text }]
			: [...prev, { role, text }];
	return { ...state, transcripts: { ...state.transcripts, [key]: next } };
}

/** Replace one instance's transcript wholesale (a history load). */
export function setTranscript(
	state: DirectChatState,
	key: string,
	msgs: DirectMsg[],
): DirectChatState {
	return { ...state, transcripts: { ...state.transcripts, [key]: msgs } };
}

export function markRunning(state: DirectChatState, target: DirectTargetRef): DirectChatState {
	const key = viewKeyOf(target);
	return state.running.includes(key) ? state : { ...state, running: [...state.running, key] };
}

export function clearRunning(state: DirectChatState, target: DirectTargetRef): DirectChatState {
	const key = viewKeyOf(target);
	return { ...state, running: state.running.filter((k) => k !== key) };
}

/** Whether the view on screen is waiting on its own reply. */
export function isViewBusy(state: DirectChatState): boolean {
	return state.running.includes(viewKeyOf(state.active));
}

/**
 * Why the composer refuses this send, or `null` when it does not.
 *
 * Only the instance you are looking at can refuse, and only for its own turn:
 * instances run on separate lanes, so one answering never blocks another or the
 * main agent. A second prompt to the *same* instance would serialise behind its
 * handle lock with no bound, so refusing says that instead of hanging.
 */
export function sendingPausedReason(state: DirectChatState): string | null {
	const { active } = state;
	if (active === null || !state.running.includes(viewKeyOf(active))) {
		return null;
	}
	return `${active.agent}/${active.handle} is still replying; you can continue once it lands`;
}
