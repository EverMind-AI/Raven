import type {
	ContentBlock,
	DataBlock,
	Msg,
	TextBlock,
	ToolCallBlock,
	ToolResultBlock,
} from '@agentscope-ai/agentscope/message';
import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

import { ConfirmCard } from './ConfirmCard';
import { FileAttachment } from './FileAttachment';
import { useSubagentNames } from './SubagentNamesContext';
import { renderToolCall } from './tool-renderers';
import { countDiffStats, DiffStats, getResultDiff } from './tool-renderers/_shared';
import { DeliverFilesInlineCard } from './tool-renderers/DeliverFilesRenderer';
import { RunSubagentDagInlineCard } from './tool-renderers/RunSubagentDagRenderer';
import { SpawnInlineCard } from './tool-renderers/SpawnInlineCard';
import { isSubagentCall, SPAWN_TOOL } from './tool-renderers/subagentCalls';
import { SubagentInlineCard } from './tool-renderers/SubagentInlineCard';
import type { TFunction, ToolCallWithResult } from './tool-renderers/types';
import { RUN_SUBAGENT_DAG_TOOL } from '@/components/dag/deriveDag';
import { DELIVER_FILES_TOOL, readManifest } from '@/components/delivery/deriveDeliverables';
import { Button } from '@/components/ui/button';
import {
	Collapsible,
	CollapsibleContent,
	CollapsibleTrigger,
} from '@/components/ui/collapsible.tsx';
import { useAudioBlock, useReplayController } from '@/context/AudioContext';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import { formatNumber, formatTime } from '@/utils/common';
import CalendarClock from '~icons/solar/alarm-bold-duotone';
import ChevronRight from '~icons/solar/alt-arrow-right-linear';
import ArrowDown from '~icons/solar/arrow-down-linear';
import ArrowUp from '~icons/solar/arrow-up-linear';
import MessageSquareQuote from '~icons/solar/chat-square-code-bold-duotone';
import CheckCircle from '~icons/solar/check-circle-bold-duotone';
import Copy from '~icons/solar/copy-bold-duotone';
import Bot from '~icons/solar/cpu-bold-duotone';
import CirclePlay from '~icons/solar/play-circle-bold-duotone';
import Loader2 from '~icons/solar/refresh-linear';
import Wrench from '~icons/solar/tuning-bold-duotone';

/**
 * A run of *consecutive* tool calls (of any name) collapsed into a single
 * container, each call paired to its result. The one aggregated summary/fold
 * renders here; every call inside is dispatched to its dedicated per-tool
 * renderer via `renderToolCall`.
 */
interface ToolCallGroupBlock {
	type: 'tool_call_group';
	calls: ToolCallWithResult[];
}

/**
 * One tool call that is rendered *inline* in the conversation flow — outside
 * the tool-chain group's collapsible summary. Used for tools whose output is
 * interactionally important (currently just ``DeliverFiles``, which surfaces
 * files the user should see immediately).
 */
interface InlineToolCallBlock {
	type: 'inline_tool_call';
	call: ToolCallWithResult;
}

type ExtendedContentBlock = ContentBlock | ToolCallGroupBlock | InlineToolCallBlock;

/**
 * Tools that the message bubble extracts out of the tool-chain group and
 * renders inline (always visible). Listed by their canonical ``name`` as it
 * appears on a ``tool_call`` block: ``DeliverFiles`` because its
 * delivered-file list is interaction-critical, and ``run_subagent_dag``
 * because its live DAG visualisation is the whole point of the call — both
 * would otherwise be hidden behind a fold.
 */
const INLINE_TOOL_NAMES: ReadonlySet<string> = new Set([DELIVER_FILES_TOOL, RUN_SUBAGENT_DAG_TOOL]);

/**
 * Pair every tool_call with its tool_result (by id) and collect *consecutive*
 * tool calls into a single `tool_call_group`, regardless of tool name — so a
 * run like `[Read, Read, Edit, some_mcp_tool]` becomes one collapsible
 * container. Non-tool blocks (text, thinking, data, ...) break the run and
 * pass through unchanged at their original position.
 *
 * Calls listed in ``INLINE_TOOL_NAMES`` (e.g. ``DeliverFiles``) — and every
 * sub-agent call (name in ``subagentNames``) — are pulled out of any group
 * and emitted as their own ``inline_tool_call`` block, so they render in the
 * conversation flow rather than inside a collapsible summary. Inline calls
 * still break the surrounding run (they are never grouped with their
 * neighbours). A call still in the ``asking`` (permission) state is left in
 * the group so the tool-chain summary can render its ``ConfirmCard``; it is
 * promoted to an inline block only once it is allowed / finished.
 *
 * Results may arrive after their calls — concurrent tool use lays content out
 * as `[call_A, call_B, result_A, result_B]` — so calls are paired by id in a
 * first pass before the run is assembled.
 */
function groupToolCalls(
	content: ContentBlock[],
	subagentNames: Set<string>,
): ExtendedContentBlock[] {
	// Pass 1: pair calls ↔ results by id; remember non-tool blocks in order.
	const callMap = new Map<string, ToolCallWithResult>();
	const orphanResults: ToolResultBlock[] = [];
	const ordering: Array<{ type: 'tool'; id: string } | { type: 'other'; block: ContentBlock }> =
		[];

	for (const block of content) {
		if (block.type === 'tool_call') {
			callMap.set(block.id, { call: block });
			ordering.push({ type: 'tool', id: block.id });
		} else if (block.type === 'tool_result') {
			const matching = callMap.get(block.id);
			if (matching) matching.result = block;
			else orphanResults.push(block);
		} else {
			ordering.push({ type: 'other', block });
		}
	}

	// Pass 2: walk the ordering, accumulating consecutive non-inline calls
	// into one group and emitting inline calls as their own blocks.
	const result: ExtendedContentBlock[] = [];
	let current: ToolCallWithResult[] = [];
	const flush = () => {
		if (current.length === 0) return;
		result.push({ type: 'tool_call_group', calls: current });
		current = [];
	};

	for (const item of ordering) {
		if (item.type === 'other') {
			flush();
			result.push(item.block);
			continue;
		}
		const entry = callMap.get(item.id);
		if (!entry) continue;
		// A delivery reached through the `tool_call` forwarder (tool-search
		// compaction) arrives under that name, so the manifest — not the tool
		// name — is what makes the call inline-worthy.
		const isInline =
			INLINE_TOOL_NAMES.has(entry.call.name) ||
			isSubagentCall(entry.call.name, subagentNames) ||
			readManifest(entry.result?.metadata) !== null;
		if (isInline && entry.call.state !== 'asking') {
			flush();
			result.push({ type: 'inline_tool_call', call: entry });
		} else {
			current.push(entry);
		}
	}
	flush();

	// Orphan results (no matching call) — surface each as its own group.
	for (const block of orphanResults) {
		result.push({
			type: 'tool_call_group',
			calls: [
				{
					call: {
						type: 'tool_call',
						id: block.id,
						name: block.name,
						input: '',
						state: 'finished' as const,
					},
					result: block,
				},
			],
		});
	}

	return result;
}

const AUDIO_WAVE_LINES: Array<{ x: number; y1: number; y2: number }> = [
	{ x: 2, y1: 10, y2: 13 },
	{ x: 6, y1: 6, y2: 17 },
	{ x: 10, y1: 3, y2: 21 },
	{ x: 14, y1: 8, y2: 15 },
	{ x: 18, y1: 5, y2: 18 },
	{ x: 22, y1: 10, y2: 13 },
];

function AudioWave({ isPlaying = true, className }: { isPlaying?: boolean; className?: string }) {
	return (
		<>
			{isPlaying && (
				<style>{`
					@keyframes audioWave {
						0%, 100% { transform: scaleY(1); }
						50%      { transform: scaleY(0.3); }
					}
				`}</style>
			)}
			<svg
				xmlns="http://www.w3.org/2000/svg"
				width="24"
				height="24"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				strokeWidth={2}
				strokeLinecap="round"
				strokeLinejoin="round"
				className={className}
			>
				{AUDIO_WAVE_LINES.map(({ x, y1, y2 }, i) => (
					<line
						key={x}
						x1={x}
						x2={x}
						y1={y1}
						y2={y2}
						style={{
							transformOrigin: `${x}px 12px`,
							animation: isPlaying
								? `audioWave 0.8s ease-in-out ${i * 0.12}s infinite`
								: 'none',
						}}
					/>
				))}
			</svg>
		</>
	);
}

/**
 * Inline audio control rendered *inside* the time/usage stat row so the play
 * icon visually merges into the same row rather than floating as its own
 * element.
 */
function AudioInlineControl({ block }: { block: DataBlock }) {
	const { t } = useTranslation();
	const audioState = useAudioBlock(block.id);
	const replayController = useReplayController();
	const audioRef = useRef<HTMLAudioElement | null>(null);
	const [isPlaying, setIsPlaying] = useState(false);

	const isStreaming = audioState?.status === 'streaming';

	// Don't build the giant base64 data URL while bytes are still streaming —
	// it would re-allocate on every DATA_BLOCK_DELTA. Live playback during
	// that window is handled by the manager's WavStreamPlayer; we only need
	// `src` for replay after the stream ends (or for historical messages).
	let src: string | null = null;
	if (!isStreaming) {
		if (audioState?.url) {
			src = audioState.url;
		} else if (block.source.type === 'url') {
			src = block.source.url;
		} else if (block.source.type === 'base64' && block.source.data) {
			src = `data:${block.source.media_type};base64,${block.source.data}`;
		}
	}

	// Reset the hidden <audio> when the source URL changes (e.g. streaming
	// just transitioned to a Blob URL). Without an explicit load() some
	// browsers keep the previous (or empty) source bound to the element.
	useEffect(() => {
		const el = audioRef.current;
		if (!el || !src) return;
		setIsPlaying(false);
		el.load();
	}, [src]);

	// Pause when a newer reply interrupts this block's playback.
	const interruptCount = audioState?.interruptCount ?? 0;
	useEffect(() => {
		if (interruptCount === 0) return;
		const el = audioRef.current;
		if (el && !el.paused) {
			el.pause();
		}
	}, [interruptCount]);

	if (isStreaming) {
		return <AudioWave isPlaying className="ml-1" />;
	}

	if (!src) return null;

	const toggle = async () => {
		const el = audioRef.current;
		if (!el) return;
		if (el.paused) {
			replayController?.play(el);
			try {
				await el.play();
			} catch (err) {
				console.error('Audio playback failed', err);
			}
		} else {
			el.pause();
			replayController?.stop();
		}
	};

	return (
		<>
			<button
				type="button"
				onClick={toggle}
				aria-label={
					isPlaying ? t('messageBubble.pauseAudio') : t('messageBubble.playAudio')
				}
				className="ml-1 inline-flex cursor-pointer items-center transition-opacity hover:opacity-70"
			>
				{isPlaying ? (
					<AudioWave isPlaying className="size-3" />
				) : (
					<CirclePlay className="size-3" />
				)}
			</button>
			<audio
				ref={audioRef}
				src={src}
				preload="auto"
				onPlay={() => setIsPlaying(true)}
				onPause={() => setIsPlaying(false)}
				onEnded={() => setIsPlaying(false)}
			/>
		</>
	);
}

type OnUserConfirm = (
	toolCallBlock: ToolCallBlock,
	confirm: boolean,
	rules?: ToolCallBlock['suggested_rules'],
) => void;

// Single underscore: registered MCP tool names are `mcp_<server>_<tool>`. This
// counter matched nothing at all while it read `mcp__` -- a spelling no raven
// build has ever registered -- so every MCP call fell through to the generic
// branch and the "N MCP tools" summary stayed at zero.
const MCP_TOOL_PREFIX = 'mcp_';

// Task-management tools are all surfaced under one "updated todos" summary.
const TODO_TOOLS = new Set(['TaskGet', 'TaskUpdate', 'TaskList', 'TaskCreate']);

function renderConfirmCard(askingCall: ToolCallBlock, onUserConfirm?: OnUserConfirm) {
	return (
		<ConfirmCard
			toolCall={askingCall}
			onUserConfirm={(confirm, rules) => {
				if (onUserConfirm) onUserConfirm(askingCall, confirm, rules);
			}}
		/>
	);
}

/**
 * Bucket a group's calls into per-category counts and total inserted/deleted
 * lines (from Edit/Write result diffs), then build the localized collapsible
 * title. Categories are appended in a fixed order — Bash, Read, Edit/Write,
 * Search (Grep/Glob), Todo, MCP — each omitted when its count is zero. If
 * nothing matches a known category, a generic "called N tools" fallback is used.
 */
function summarizeToolGroup(calls: ToolCallWithResult[], t: TFunction, subagentNames: Set<string>) {
	let nBash = 0;
	let nRead = 0;
	let nEdit = 0;
	let nSearch = 0;
	let nTodo = 0;
	let nMCP = 0;
	let nSubagent = 0;
	let nDag = 0;
	let insertions = 0;
	let deletions = 0;

	for (const { call, result } of calls) {
		const name = call.name;
		if (isSubagentCall(name, subagentNames)) {
			nSubagent += 1;
		} else if (name === 'Bash') {
			nBash += 1;
		} else if (name === 'Read') {
			nRead += 1;
		} else if (name === 'Edit' || name === 'Write') {
			nEdit += 1;
			// Sum the real +/- line changes from the backend-provided diff.
			const diff = result ? getResultDiff(result) : undefined;
			if (diff) {
				const stats = countDiffStats(diff);
				insertions += stats.insertions;
				deletions += stats.deletions;
			}
		} else if (name === 'Grep' || name === 'Glob') {
			nSearch += 1;
		} else if (TODO_TOOLS.has(name)) {
			nTodo += 1;
		} else if (name === RUN_SUBAGENT_DAG_TOOL) {
			nDag += 1;
		} else if (name.startsWith(MCP_TOOL_PREFIX)) {
			nMCP += 1;
		}
	}

	const parts: string[] = [];
	if (nBash > 0) parts.push(t('tool.summary.bash', { count: nBash }));
	if (nRead > 0) parts.push(t('tool.summary.read', { count: nRead }));
	if (nEdit > 0) parts.push(t('tool.summary.edit', { count: nEdit }));
	if (nSearch > 0) parts.push(t('tool.summary.search', { count: nSearch }));
	if (nTodo > 0) parts.push(t('tool.summary.todo', { count: nTodo }));
	if (nMCP > 0) parts.push(t('tool.summary.mcp', { count: nMCP }));
	if (nDag > 0) parts.push(t('tool.summary.dag', { count: nDag }));
	if (nSubagent > 0) parts.push(t('tool.summary.subagent', { count: nSubagent }));

	const joined =
		parts.length > 0
			? parts.join(t('tool.summary.separator'))
			: t('tool.summary.fallback', { count: calls.length });
	// Sentence-case only the very first letter (each i18n part is lower-cased
	// so commas don't introduce mid-sentence capitals in English; a no-op for
	// scripts without letter case such as Chinese).
	const title = joined.length > 0 ? joined[0].toUpperCase() + joined.slice(1) : joined;

	return { title, insertions, deletions };
}

/**
 * Render a single content block. A `tool_call_group` renders one summary fold
 * whose expanded body dispatches each call to `renderToolCall`. Truncation at
 * the first `asking` call (and the trailing ConfirmCard, kept outside the fold)
 * lives here so renderers only ever see a clean list of calls.
 *
 * An `inline_tool_call` block renders its call directly in the conversation
 * flow (no surrounding collapsible summary) — see ``INLINE_TOOL_NAMES``.
 * ``run_subagent_dag`` is one such tool: its live DAG visualisation renders
 * as an always-open inline card so the user sees it without an extra click.
 * A sub-agent call (name in ``subagentNames``) renders as a
 * ``SubagentInlineCard`` — collapsed by default with a cleaned response
 * preview, expanding to the full prompt + response on click.
 */
function renderBlock(
	block: ExtendedContentBlock,
	index: number,
	t: TFunction,
	subagentNames: Set<string>,
	onUserConfirm?: (
		toolCallBlock: ToolCallBlock,
		confirm: boolean,
		rules?: ToolCallBlock['suggested_rules'],
	) => void,
) {
	switch (block.type) {
		case 'inline_tool_call': {
			if (block.call.call.name === RUN_SUBAGENT_DAG_TOOL) {
				return (
					<div key={index} className="my-1">
						<RunSubagentDagInlineCard pair={block.call} t={t} />
					</div>
				);
			}
			// `spawn` is deliberately not a SubagentInlineCard: that card puts the
			// tool result under a "Response" heading, and `spawn` returns an
			// acknowledgement, not the sub-agent's reply (which arrives later as
			// its own announce turn). Its own card shows the delegated task and
			// leaves the ack labelled as a tool result.
			if (block.call.call.name === SPAWN_TOOL) {
				return (
					<div key={index} className="my-1">
						<SpawnInlineCard pair={block.call} t={t} />
					</div>
				);
			}
			if (subagentNames.has(block.call.call.name)) {
				return (
					<div key={index} className="my-1">
						<SubagentInlineCard pair={block.call} t={t} />
					</div>
				);
			}
			const card = DeliverFilesInlineCard({ pair: block.call, t });
			if (card !== undefined) {
				return (
					<div key={index} className="my-1">
						{card}
					</div>
				);
			}
			// No manifest yet (still streaming) — fall through to the tool
			// renderer so the user sees something while waiting.
			return (
				<div key={index} className="my-1">
					{renderToolCall(block.call, t, subagentNames)}
				</div>
			);
		}
		case 'tool_call_group': {
			const { title, insertions, deletions } = summarizeToolGroup(
				block.calls,
				t,
				subagentNames,
			);
			// Truncate at (and including) the first `asking` call — nothing runs
			// after it. Its ConfirmCard renders OUTSIDE the Collapsible so
			// collapsing never hides an action the user still needs to take.
			const askIdx = block.calls.findIndex((c) => c.call.state === 'asking');
			const visible = askIdx === -1 ? block.calls : block.calls.slice(0, askIdx + 1);
			const askingCall = askIdx === -1 ? null : block.calls[askIdx].call;

			const allFinished = block.calls.some((c) => !c.result || c.result.state === 'running');
			return (
				<div key={index} className="flex flex-col gap-y-4 text-muted-foreground">
					<Collapsible>
						<CollapsibleTrigger asChild>
							<Button
								variant="ghost"
								className="group flex h-auto w-full items-center justify-between gap-2 px-0 py-1 hover:bg-transparent data-[state=open]:bg-transparent active:!translate-y-0 cursor-pointer"
							>
								<span
									className={cn(
										'flex min-w-0 items-center gap-1.5',
										allFinished && 'shimmer',
									)}
									style={
										{ '--shimmer-color': 'var(--glow)' } as React.CSSProperties
									}
								>
									<ChevronRight className="size-3 shrink-0 text-gold/80 transition-transform group-hover:text-gold group-data-[state=open]:rotate-90" />
									<span className="truncate text-xs font-medium uppercase tracking-wide text-gold">
										{title}
									</span>
								</span>
								{(insertions > 0 || deletions > 0) && (
									<DiffStats insertions={insertions} deletions={deletions} />
								)}
							</Button>
						</CollapsibleTrigger>
						<CollapsibleContent className="mt-1 flex flex-col gap-y-1 rounded bg-gold-olive/10 p-2.5 text-sm">
							{visible.map((pair) => renderToolCall(pair, t, subagentNames))}
						</CollapsibleContent>
					</Collapsible>
					{askingCall && renderConfirmCard(askingCall, onUserConfirm)}
				</div>
			);
		}
		case 'text':
			return (
				<div key={index} className="prose w-full min-w-full">
					<ReactMarkdown
						remarkPlugins={[remarkGfm]}
						components={{
							code: ({ className, children, ...props }) => {
								const isInline = !String(className ?? '').startsWith('language-');
								if (isInline) {
									return (
										<code className={`${className ?? ''} break-all`} {...props}>
											{children}
										</code>
									);
								}
								return (
									<div className="relative w-full">
										<Button
											size="icon-xs"
											variant="ghost"
											className="absolute top-0 right-0 z-10"
											onClick={async (e) => {
												e.preventDefault();
												e.stopPropagation();
												await navigator.clipboard.writeText(
													String(children),
												);
											}}
										>
											<Copy />
										</Button>
										<div className="overflow-x-auto max-w-full w-full">
											<code className={className} {...props}>
												{children}
											</code>
										</div>
									</div>
								);
							},
						}}
					>
						{block.text}
					</ReactMarkdown>
				</div>
			);

		case 'thinking':
			return (
				<Collapsible key={index} className="text-muted-foreground">
					<CollapsibleTrigger asChild>
						<Button
							variant="ghost"
							className="group flex h-auto w-full items-center justify-start gap-1.5 px-0 py-1 hover:bg-transparent data-[state=open]:bg-transparent active:!translate-y-0 cursor-pointer"
						>
							<ChevronRight className="size-3 shrink-0 text-muted-foreground/70 transition-transform group-hover:text-muted-foreground group-data-[state=open]:rotate-90" />
							<span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
								{t('messageBubble.thinking')}
							</span>
						</Button>
					</CollapsibleTrigger>
					<CollapsibleContent className="mt-1 rounded bg-gold-olive/10 p-2.5">
						<p className="whitespace-pre-wrap text-sm italic">{block.thinking}</p>
					</CollapsibleContent>
				</Collapsible>
			);

		case 'data': {
			const dataType = block.source.media_type.split('/')[0];
			// Audio data blocks render in the footer (see AudioFooterControl),
			// not inline alongside text.
			if (dataType === 'audio') return null;
			const data =
				block.source.type === 'url'
					? block.source.url
					: `data:${block.source.media_type};base64,${block.source.data}`;
			switch (dataType) {
				case 'image':
					return (
						<img
							key={index}
							src={data}
							alt={block.name || 'Uploaded image'}
							className="max-h-80 max-w-full rounded-lg object-contain"
						/>
					);
				case 'video':
					return (
						<video
							key={index}
							controls
							src={data}
							className="max-h-80 max-w-full rounded-lg"
						/>
					);
				default:
					return (
						<FileAttachment
							key={index}
							name={block.name}
							href={data}
							mediaType={block.source.media_type}
						/>
					);
			}
		}

		case 'hint': {
			// Parse source: try JSON, fall back to plain string, default to t('common.message').
			let hintLabel: string;
			let hintSublabel: string | null = null;
			let HintIcon = MessageSquareQuote;

			if (block.source) {
				try {
					const parsed = JSON.parse(block.source) as {
						label?: string;
						sublabel?: string;
					};
					hintLabel = parsed.label
						? t(`messageBubble.hintSource.${parsed.label}`)
						: block.source;
					hintSublabel = parsed.sublabel ?? null;
					if (parsed.label === 'team_message') HintIcon = Bot;
					else if (parsed.label === 'subagent_response') HintIcon = Bot;
					else if (parsed.label === 'schedule') HintIcon = CalendarClock;
					else if (parsed.label === 'tool_output') HintIcon = Wrench;
				} catch {
					hintLabel = block.source;
				}
			} else {
				hintLabel = t('common.message');
			}
			const items: (TextBlock | DataBlock)[] =
				typeof block.hint === 'string'
					? [{ type: 'text', id: `${block.id}-text`, text: block.hint }]
					: block.hint;
			return (
				<Collapsible className="max-w-full text-muted-foreground">
					<CollapsibleTrigger asChild>
						<Button
							variant="ghost"
							className="group flex h-auto w-full max-w-full items-center justify-start gap-1.5 px-0 py-1 hover:bg-transparent data-[state=open]:bg-transparent active:!translate-y-0 cursor-pointer"
						>
							<ChevronRight className="size-3 shrink-0 text-gold/80 transition-transform group-hover:text-gold group-data-[state=open]:rotate-90" />
							<HintIcon className="size-3.5 shrink-0" />
							<span className="text-xs font-medium uppercase tracking-wide text-gold">
								{hintLabel}
							</span>
							{hintSublabel && (
								<span className="text-muted-foreground truncate text-xs font-normal">
									{hintSublabel}
								</span>
							)}
						</Button>
					</CollapsibleTrigger>
					<CollapsibleContent className="mt-1 max-w-full overflow-hidden rounded bg-gold-olive/10 p-2.5 break-all text-muted-foreground">
						{items.map((inner, i) => renderBlock(inner, i, t, subagentNames))}
					</CollapsibleContent>
				</Collapsible>
			);
		}

		default:
			return null;
	}
}

interface MessageBubbleProps {
	message: Msg;
	onUserConfirm: (
		toolCallBlock: ToolCallBlock,
		confirm: boolean,
		replyId: string,
		rules?: ToolCallBlock['suggested_rules'],
	) => void;
}

/**
 * A message bubble component that displays a chat message.
 *
 * Running state is derived from `message.finished_at`: a missing or null
 * `finished_at` means the agent is still producing this reply. The bottom
 * status row shows a single left-aligned badge laid out as
 * `[state-icon] [duration] [↑in ↓out]`:
 *   - State icon: spinning `Loader2` while running, static `CheckCircle`
 *     once finished.
 *   - Duration is `now - created_at` while running (ticking each second),
 *     `finished_at - created_at` once complete.
 *   - Token counts only appear once `usage` is populated with non-zero
 *     values — typically after the message finishes.
 *
 * When `content` is empty and the message is still running, the bubble
 * body is omitted entirely so only the bottom status row renders.
 */
export function MessageBubble({ message, onUserConfirm }: MessageBubbleProps) {
	const isUser = message.role === 'user';
	const { t } = useTranslation();
	const subagentNames = useSubagentNames();

	const isRunning = !message.finished_at;
	const hasUsage =
		!!message.usage &&
		((message.usage.input_tokens ?? 0) > 0 || (message.usage.output_tokens ?? 0) > 0);

	// Tick once per second while running so the elapsed time updates live.
	const [now, setNow] = useState(() => Date.now());
	useEffect(() => {
		if (!isRunning) return;
		const id = setInterval(() => setNow(Date.now()), 1000);
		return () => clearInterval(id);
	}, [isRunning]);

	const blocks = groupToolCalls(message.content, subagentNames);
	const audioBlocks = message.content.filter(
		(b): b is DataBlock => b.type === 'data' && b.source.media_type.split('/')[0] === 'audio',
	);
	// Audio data blocks are rendered in the footer, so they shouldn't keep an
	// otherwise-empty body bubble alive.
	const hasBodyContent = blocks.some(
		(b) => !(b.type === 'data' && b.source.media_type.split('/')[0] === 'audio'),
	);
	const showBody = hasBodyContent;
	const showFooter = !isUser;

	const startMs = new Date(message.created_at).getTime();
	const endMs = isRunning ? now : new Date(message.finished_at!).getTime();
	const elapsedSeconds = Math.max(0, (endMs - startMs) / 1000);
	const elapsedText = formatTime(elapsedSeconds);

	return (
		<div
			className={`flex flex-col w-full max-w-full ${isUser ? 'items-end' : 'items-start'} mb-4`}
		>
			{showBody && (
				<div
					className={`p-4 rounded-xl space-y-2 max-w-full ${
						isUser ? 'w-fit bg-secondary' : 'w-full min-w-full'
					}`}
				>
					{blocks.map((block, i) =>
						renderBlock(
							block,
							i,
							t,
							subagentNames,
							(
								toolCall: ToolCallBlock,
								confirm: boolean,
								rules?: ToolCallBlock['suggested_rules'],
							) => {
								onUserConfirm(toolCall, confirm, message.id, rules);
								toolCall.state = confirm ? 'allowed' : 'finished';
							},
						),
					)}
				</div>
			)}
			{showFooter && (
				<div
					className="flex w-full flex-row items-center gap-x-1.5 border-t border-border px-2 pt-1.5 text-xs text-muted-foreground"
					aria-label={isRunning ? t('messageBubble.running') : undefined}
				>
					{isRunning ? (
						<Loader2 data-icon="inline-start" className="size-3 animate-spin" />
					) : (
						<CheckCircle data-icon="inline-start" className="size-3" />
					)}
					<span className="tabular-nums tracking-tighter text-gold">{elapsedText}</span>
					{hasUsage && (
						<>
							<span aria-hidden="true">·</span>
							<ArrowUp data-icon="inline-start" className="size-3" />
							<span className="tabular-nums text-gold">
								{formatNumber(message.usage?.input_tokens ?? 0)}
							</span>
							<span aria-hidden="true">·</span>
							<ArrowDown data-icon="inline-start" className="size-3" />
							<span className="tabular-nums text-gold">
								{formatNumber(message.usage?.output_tokens ?? 0)}
							</span>
						</>
					)}
					{audioBlocks.map((block) => (
						<AudioInlineControl key={block.id} block={block} />
					))}
				</div>
			)}
		</div>
	);
}
