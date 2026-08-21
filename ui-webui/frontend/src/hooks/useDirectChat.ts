import { AssistantMsg, UserMsg } from '@agentscope-ai/agentscope/message';
import type { ContentBlock, Msg } from '@agentscope-ai/agentscope/message';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ravenConfigApi } from '@/api/ravenConfig';
import type { RavenInstanceTurn } from '@/api/ravenConfig';
import type { DirectMsg, DirectTargetRef } from '@/components/chat/directChatStore';
import {
	appendDelta,
	clearRunning,
	directKey,
	emptyDirectChat,
	markRunning,
	sendingPausedReason,
	setTranscript,
	applyLive,
	viewKeyOf,
} from '@/components/chat/directChatStore';

/** One stored row as a transcript message. */
const toDirectMsg = (t: RavenInstanceTurn): DirectMsg => ({
	role: t.role,
	text: t.content,
	reasoning: t.reasoning_content,
	toolCalls: t.tool_calls?.map((c) => ({ id: c.id, name: c.name, input: c.arguments })),
	toolCallId: t.tool_call_id,
});

/**
 * How often the instance on screen is re-read while it is answering. Its steps
 * live only in the runtime's in-flight activity, so this is the one thing that
 * makes them appear before the turn ends.
 */
const STEP_POLL_MS = 400;

/**
 * The chat area's direct-chat mode: which instance it is showing, what that
 * instance has said, and how to say something back.
 *
 * The rules live in `directChatStore` as pure functions; this hook is the React
 * binding plus the two I/O edges (the send, and the history load). Keeping them
 * apart is what lets the rules be tested without a renderer -- and they are the
 * same rules the TUI encodes, so a divergence would show up as two surfaces
 * disagreeing about whose turn it is.
 */
export function useDirectChat(
	sessionId: string | null,
	/**
	 * Whether an instance has a turn in flight, from the caller's own status map.
	 *
	 * Passed in because the two signals live apart: `state.running` is turns sent
	 * from here, and a `spawn` the main agent made -- or a DAG node -- is reported
	 * only on the instance-status stream the page owns. Reading only the former is
	 * what left a spawned turn showing nothing while it worked.
	 */
	isInstanceWorking?: (target: DirectTargetRef) => boolean,
) {
	const [state, setState] = useState(emptyDirectChat);
	// Which instances have had their history read, so re-entering a view does
	// not re-fetch (and cannot overwrite deltas that arrived since).
	const loaded = useRef<Set<string>>(new Set());

	/**
	 * Read one instance's conversation and put it on screen.
	 *
	 * The three reads are the ones the TUI encodes, deliberately: `enter` on
	 * switching in, which declines to overwrite anything that arrived since;
	 * `settled` when a turn ends, which replaces the view with what has now been
	 * written down; `live` while a turn runs, which splices in the steps no
	 * record holds yet. One reader for all three, so what is on screen while a
	 * turn runs and what replaces it when the turn lands cannot drift apart.
	 */
	const syncHistory = useCallback(
		async (target: DirectTargetRef, read: 'enter' | 'live' | 'settled' = 'enter') => {
			const key = directKey(target.agent, target.handle);
			if (!sessionId || (read === 'enter' && loaded.current.has(key))) {
				return;
			}
			if (read === 'enter') {
				loaded.current.add(key);
			}
			try {
				const r = await ravenConfigApi.instanceHistory(
					sessionId,
					target.agent,
					target.handle,
				);
				const turns = r?.turns ?? [];
				const settled = turns.filter((t) => !t.live).map(toDirectMsg);
				if (read === 'live') {
					setState((s) =>
						applyLive(s, key, settled, turns.filter((t) => t.live).map(toDirectMsg)),
					);
					return;
				}
				// On entering, only when nothing arrived meanwhile: a reply that
				// streamed in during the fetch is newer than the record it would
				// replace. A settled read is the opposite -- it exists to replace
				// exactly that.
				setState((s) =>
					read === 'enter' && (s.transcripts[key] ?? []).length > 0
						? s
						: setTranscript(s, key, settled),
				);
			} catch {
				// Best-effort: an unreadable record must not block the chat.
				loaded.current.delete(key);
			}
		},
		[sessionId],
	);

	/** Apply one `subagent_direct_*` custom event. */
	const onDirectEvent = useCallback((name: string, value: Record<string, unknown>) => {
		const target = value.target as DirectTargetRef | undefined;
		if (!target?.agent || !target?.handle) {
			return;
		}
		const key = directKey(target.agent, target.handle);
		if (name === 'subagent_direct_delta') {
			const text = String(value.text ?? '');
			if (text) {
				setState((s) => appendDelta(s, key, 'assistant', text));
			}
			return;
		}
		if (name === 'subagent_direct_complete') {
			setState((s) => {
				// The whole reply, for a transport that could not stream it: the
				// runtime sends the text on completion either way, so appending
				// it unconditionally would render a streamed reply twice.
				const seen = (s.transcripts[key] ?? []).at(-1);
				const whole = String(value.text ?? '');
				const streamed = seen?.role === 'assistant' && seen.text.length > 0;
				const next = !streamed && whole ? appendDelta(s, key, 'assistant', whole) : s;
				return clearRunning(next, target);
			});
			// The record now holds this turn, steps and all. Until it is read
			// back the view shows the last live snapshot of those steps; this is
			// what replaces it with the settled account. Re-armed because the
			// load guard is what would otherwise refuse the second read.
			void syncHistory(target, 'settled');
			return;
		}
		if (name === 'subagent_direct_error') {
			setState((s) =>
				clearRunning(appendDelta(s, key, 'system', String(value.error ?? 'failed')), target),
			);
		}
	}, [syncHistory]);

	/** Switch the chat area to an instance, loading its record once. */
	const enter = useCallback(
		async (target: DirectTargetRef) => {
			setState((s) => ({ ...s, active: target }));
			await syncHistory(target);
		},
		[syncHistory],
	);

	/** Back to the main agent. */
	const leave = useCallback(() => setState((s) => ({ ...s, active: null })), []);

	// While the instance on screen is answering, re-read its steps. They exist
	// only in the activity the runtime is collecting -- nothing of them reaches
	// disk until the turn lands -- and the event stream carries the reply text
	// alone, so a read is the only way they can appear before the end.
	//
	// Only the view on screen; a turn the user is not looking at is caught by the
	// settled read when it ends.
	const active = state.active;
	const busy =
		active !== null &&
		(state.running.includes(viewKeyOf(active)) || (isInstanceWorking?.(active) ?? false));

	useEffect(() => {
		if (!busy || active === null) {
			return;
		}
		// At once as well as on the interval: a view opened mid-turn would
		// otherwise show nothing for the first tick.
		void syncHistory(active, 'live');
		const id = setInterval(() => void syncHistory(active, 'live'), STEP_POLL_MS);
		return () => clearInterval(id);
	}, [active, busy, syncHistory]);

	// One read after the work stops, per view. The poll is gone by then, and a
	// spawn or a DAG node sends no `subagent_direct_complete` either -- the
	// runtime tags those events on direct turns only.
	const settledFor = useRef<Record<string, boolean>>({});

	useEffect(() => {
		if (active === null) {
			return;
		}
		const key = viewKeyOf(active);
		const was = settledFor.current[key] ?? false;
		settledFor.current[key] = busy;
		if (was && !busy) {
			void syncHistory(active, 'settled');
		}
	}, [active, busy, syncHistory]);

	const send = useCallback(
		async (text: string) => {
			const target = state.active;
			if (!target || !sessionId || !text.trim()) {
				return;
			}
			const key = viewKeyOf(target);
			setState((s) => markRunning(appendDelta(s, key, 'user', text), target));
			try {
				await ravenConfigApi.chatWithInstance({
					session_id: sessionId,
					agent: target.agent,
					handle: target.handle,
					content: text,
				});
			} catch (e) {
				setState((s) =>
					clearRunning(appendDelta(s, key, 'system', String(e)), target),
				);
			}
		},
		[sessionId, state.active],
	);

	/**
	 * The active instance's transcript in the shape the message bubbles render,
	 * so a direct chat looks like every other conversation rather than needing
	 * its own renderer.
	 *
	 * A turn is more than its text: the thought becomes a thinking block, each
	 * tool call a tool_call block, and a `role: "tool"` row the tool_result that
	 * closes one. The direct chat used to flatten every turn to a text bubble,
	 * which is what made it show only the two ends of an exchange.
	 *
	 * A tool result is folded into the message that made the call rather than
	 * becoming a message of its own, because that is how the renderer pairs the
	 * two: `appendEvent` pushes both blocks onto one message and `findBlock`
	 * looks the partner up *within* it. Its `name` comes from that call for the
	 * same reason -- the row itself only carries the id it answers.
	 */
	const viewMsgs: Msg[] = useMemo(() => {
		if (state.active === null) {
			return [];
		}
		const key = viewKeyOf(state.active);
		const name = `${state.active.agent}/${state.active.handle}`;
		const msgs: Msg[] = [];
		const calls = new Map<string, string>();
		let openAssistant: Msg | null = null;

		(state.transcripts[key] ?? []).forEach((m, i) => {
			if (m.role === 'user') {
				openAssistant = null;
				msgs.push(
					UserMsg({
						name: 'user',
						content: [{ id: `${key}:u${i}`, type: 'text', text: m.text }],
					}),
				);
				return;
			}
			if (m.role === 'tool') {
				const id = m.toolCallId ?? `${key}:r${i}`;
				const block: ContentBlock = {
					id,
					type: 'tool_result',
					name: calls.get(id) ?? '',
					output: m.text,
					// A replayed result is settled. The acp collector is what
					// marks a failed one, prefixing its content exactly so.
					state: m.text.startsWith('[failed]') ? 'error' : 'success',
				};
				// Its own message only when nothing is open to fold it into --
				// a record whose calling turn was trimmed away still shows what
				// came back.
				if (openAssistant) {
					openAssistant.content.push(block);
				} else {
					msgs.push(AssistantMsg({ id: `${key}:${i}`, name, content: [block] }));
				}
				return;
			}
			const content: ContentBlock[] = [];
			if (m.reasoning) {
				content.push({ id: `${key}:t${i}`, type: 'thinking', thinking: m.reasoning });
			}
			if (m.text) {
				content.push({ id: `${key}:a${i}`, type: 'text', text: m.text });
			}
			for (const call of m.toolCalls ?? []) {
				calls.set(call.id, call.name);
				content.push({
					id: call.id,
					type: 'tool_call',
					name: call.name,
					input: call.input,
					// Replayed, so it is over: anything else would draw a card
					// still waiting on a permission nobody is going to answer.
					state: 'finished',
				});
			}
			const msg = AssistantMsg({ id: `${key}:${i}`, name, content });
			msgs.push(msg);
			// Only a turn that actually called something can take a result.
			openAssistant = (m.toolCalls ?? []).length > 0 ? msg : null;
		});
		return msgs;
	}, [state.active, state.transcripts]);

	return {
		active: state.active,
		enter,
		leave,
		onDirectEvent,
		pausedReason: sendingPausedReason(state),
		send,
		viewMsgs,
	};
}
