import {
	AiHubMix,
	Anthropic,
	Azure,
	Codex,
	DeepSeek,
	Gemini,
	GithubCopilot,
	Groq,
	Minimax,
	Moonshot,
	Ollama,
	OpenAI,
	OpenRouter,
	Qwen,
	SiliconCloud,
	Vllm,
	Volcengine,
	ZAI,
} from '@lobehub/icons';
import type { ReactNode } from 'react';

import ServerIcon from '~icons/solar/server-square-bold-duotone';

interface ProviderIconProps {
	/** Raven provider slug, e.g. `openai`, `azure_openai`, `custom`. */
	type: string;
	size?: number;
}

// Brand logo for each raven provider slug. The slugs are the canonical `name` of
// each entry in raven.providers.registry.PROVIDERS -- which is what
// /raven/providers returns -- so a registry rename lands here too; the registry's
// `name_aliases` never reach the wire. `custom` is the generic OpenAI-compatible
// endpoint (uses the OpenAI mark); anything unmapped falls back to a neutral
// server glyph.
export function ProviderIcon({ type, size = 20 }: ProviderIconProps) {
	let inner: ReactNode;
	switch (type) {
		case 'openai':
		case 'custom':
			inner = <OpenAI.Avatar size={size} />;
			break;
		case 'openai_codex':
			inner = <Codex.Avatar size={size} />;
			break;
		case 'anthropic':
			inner = <Anthropic.Avatar size={size} />;
			break;
		case 'gemini':
			inner = <Gemini.Avatar size={size} />;
			break;
		case 'deepseek':
			inner = <DeepSeek.Avatar size={size} />;
			break;
		case 'moonshot':
			inner = <Moonshot.Avatar size={size} />;
			break;
		case 'dashscope':
			inner = <Qwen.Avatar size={size} />;
			break;
		case 'ollama_chat':
			inner = <Ollama.Avatar size={size} />;
			break;
		case 'openrouter':
			inner = <OpenRouter.Avatar size={size} />;
			break;
		case 'groq':
			inner = <Groq.Avatar size={size} />;
			break;
		case 'zai':
			inner = <ZAI.Avatar size={size} />;
			break;
		case 'minimax':
		case 'minimax_global':
		case 'minimax_cn':
			inner = <Minimax.Avatar size={size} />;
			break;
		case 'aihubmix':
			inner = <AiHubMix.Avatar size={size} />;
			break;
		case 'siliconflow':
			inner = <SiliconCloud.Avatar size={size} />;
			break;
		case 'volcengine':
			inner = <Volcengine.Avatar size={size} />;
			break;
		case 'azure_openai':
			inner = <Azure.Avatar size={size} />;
			break;
		case 'github_copilot':
			inner = <GithubCopilot.Avatar size={size} />;
			break;
		case 'hosted_vllm':
			inner = <Vllm.Avatar size={size} />;
			break;
		default:
			inner = <ServerIcon className="size-full text-muted-foreground" />;
	}
	// Warm square tile framing the brand mark; `[&_svg]:!size-full` overrides the
	// ambient sidebar rule `[&_svg]:size-4` so the lobehub glyph fills its tile at
	// any size. The lobehub `.Avatar` draws its own circular brand badge inside
	// this box, so the tile's corners show the warm background around it.
	return (
		<span
			className="icon-mono bg-muted inline-flex shrink-0 items-center justify-center rounded-2xl [&_svg]:!size-full"
			style={{ width: size, height: size }}
		>
			{inner}
		</span>
	);
}
