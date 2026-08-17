import { AssistantMsg, UserMsg } from '@agentscope-ai/agentscope/message';
import type { Msg } from '@agentscope-ai/agentscope/message';
import { useCallback, useMemo, useRef, useState } from 'react';

import { ravenConfigApi } from '@/api/ravenConfig';
import type { DirectMsg, DirectTargetRef } from '@/components/chat/directChatStore';
import {
	appendDelta,
	clearRunning,
	directKey,
	emptyDirectChat,
	markRunning,
	sendingPausedReason,
	setTranscript,
	viewKeyOf,
} from '@/components/chat/directChatStore';

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
export function useDirectChat(sessionId: string | null) {
	const [state, setState] = useState(emptyDirectChat);
	// Which instances have had their history read, so re-entering a view does
	// not re-fetch (and cannot overwrite deltas that arrived since).
	const loaded = useRef<Set<string>>(new Set());

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
			return;
		}
		if (name === 'subagent_direct_error') {
			setState((s) =>
				clearRunning(appendDelta(s, key, 'system', String(value.error ?? 'failed')), target),
			);
		}
	}, []);

	/** Switch the chat area to an instance, loading its record once. */
	const enter = useCallback(
		async (target: DirectTargetRef) => {
			setState((s) => ({ ...s, active: target }));
			const key = directKey(target.agent, target.handle);
			if (!sessionId || loaded.current.has(key)) {
				return;
			}
			loaded.current.add(key);
			try {
				const r = await ravenConfigApi.instanceHistory(
					sessionId,
					target.agent,
					target.handle,
				);
				const turns: DirectMsg[] = (r?.turns ?? []).map((t) => ({
					role: t.role,
					text: t.content,
				}));
				// Only when nothing arrived meanwhile: a reply that streamed in
				// during the fetch is newer than the record it would replace.
				setState((s) =>
					(s.transcripts[key] ?? []).length > 0 ? s : setTranscript(s, key, turns),
				);
			} catch {
				// Best-effort: an unreadable record must not block the chat.
				loaded.current.delete(key);
			}
		},
		[sessionId],
	);

	/** Back to the main agent. */
	const leave = useCallback(() => setState((s) => ({ ...s, active: null })), []);

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
	 */
	const viewMsgs: Msg[] = useMemo(() => {
		if (state.active === null) {
			return [];
		}
		const key = viewKeyOf(state.active);
		return (state.transcripts[key] ?? []).map((m, i) =>
			m.role === 'user'
				? UserMsg({
						name: 'user',
						content: [{ id: `${key}:u${i}`, type: 'text', text: m.text }],
					})
				: AssistantMsg({
						id: `${key}:${i}`,
						name: `${state.active!.agent}/${state.active!.handle}`,
						content: [{ id: `${key}:a${i}`, type: 'text', text: m.text }],
					}),
		);
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
