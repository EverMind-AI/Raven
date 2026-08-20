import { ClaudeCode, Codex, HermesAgent, OpenAI, OpenClaw, OpenCode } from '@lobehub/icons';
import type { ReactNode } from 'react';

import { MiroMindMark } from '@/components/MiroMindMark';
import { cn } from '@/lib/utils';
import Terminal from '~icons/solar/command-bold-duotone';
import Bot from '~icons/solar/cpu-bold-duotone';

interface SubagentIconProps {
	/** A preset `name`, or a custom subagent's kind (`cli` / `openai`). */
	type: string;
	size?: number;
}

/** Brand mark per built-in preset name. Anything unmapped - every custom
 *  subagent, and any preset added to presets.py without an entry here - falls
 *  back to a neutral glyph rather than rendering nothing. */
export function SubagentIcon({ type, size = 20 }: SubagentIconProps) {
	let inner: ReactNode;
	// Brand marks must keep their own colours; the duotone glyphs must not, so
	// they keep the app-wide gold accent the global rule in index.css applies.
	let isBrandMark = true;
	switch (type) {
		case 'claude_code':
			// The Claude Code product mark, not `Anthropic` (the company) or
			// `Claude` (the model family) - all three are separate marks.
			inner = <ClaudeCode.Avatar size={size} />;
			break;
		case 'codex':
			inner = <Codex.Avatar size={size} />;
			break;
		case 'openclaw':
			inner = <OpenClaw.Avatar size={size} />;
			break;
		case 'hermes':
			inner = <HermesAgent.Avatar size={size} />;
			break;
		case 'opencode':
			inner = <OpenCode.Avatar size={size} />;
			break;
		case 'mirothinker':
			inner = <MiroMindMark />;
			break;
		case 'openai':
			inner = <OpenAI.Avatar size={size} />;
			break;
		case 'cli':
			inner = <Terminal />;
			isBrandMark = false;
			break;
		default:
			inner = <Bot />;
			isBrandMark = false;
	}
	// Same tile as ProviderIcon on /credential, and for the same reasons: the box
	// is sized explicitly because the ambient sidebar rule `[&_svg]:size-4` would
	// otherwise collapse an unsized wrapper to 16px while a lobehub Avatar carries
	// its own inline size, leaving two icon geometries in one list. `!size-full`
	// beats that rule so the glyph fills the tile at any size, and
	// `overflow-hidden` makes the tile the single source of the corner radius, so
	// a mark that draws its own backing plate cannot square off the row.
	return (
		<span
			className={cn(
				'bg-muted inline-flex shrink-0 items-center justify-center overflow-hidden rounded-2xl [&_svg]:!size-full',
				isBrandMark && 'icon-mono',
			)}
			style={{ width: size, height: size }}
		>
			{inner}
		</span>
	);
}
