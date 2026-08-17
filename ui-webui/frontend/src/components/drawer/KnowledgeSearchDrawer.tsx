import { useState } from 'react';

import type { VectorSearchResult } from '@/api';
import { Badge } from '@/components/ui/badge.tsx';
import { Button } from '@/components/ui/button.tsx';
import {
	Empty,
	EmptyDescription,
	EmptyHeader,
	EmptyMedia,
	EmptyTitle,
} from '@/components/ui/empty.tsx';
import { Input } from '@/components/ui/input.tsx';
import { Label } from '@/components/ui/label.tsx';
import {
	Sheet,
	SheetContent,
	SheetDescription,
	SheetFooter,
	SheetHeader,
	SheetTitle,
} from '@/components/ui/sheet.tsx';
import { Textarea } from '@/components/ui/textarea.tsx';
import { useKnowledgeBases } from '@/hooks/useKnowledgeBases';
import { useTranslation } from '@/i18n/useI18n.ts';
import { cn } from '@/lib/utils';
import ChevronRight from '~icons/solar/alt-arrow-right-linear';
import Search from '~icons/solar/magnifer-bold-duotone';
import Loader2 from '~icons/solar/refresh-linear';
import FlaskConical from '~icons/solar/test-tube-bold-duotone';

interface Props {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	knowledgeBaseId: string;
	knowledgeBaseName: string;
}

/**
 * Right-side drawer for ad-hoc retrieval testing on a knowledge base.
 * The user types a query, picks `top_k`, and inspects ranked chunks.
 */
export function KnowledgeSearchDrawer({
	open,
	onOpenChange,
	knowledgeBaseId,
	knowledgeBaseName,
}: Props) {
	const { t } = useTranslation();
	const { search } = useKnowledgeBases();
	const [query, setQuery] = useState('');
	const [topK, setTopK] = useState(5);
	const [loading, setLoading] = useState(false);
	const [results, setResults] = useState<VectorSearchResult[] | null>(null);
	const [error, setError] = useState<string | null>(null);

	const handleSearch = async () => {
		const trimmed = query.trim();
		if (!trimmed) return;
		setLoading(true);
		setError(null);
		try {
			const res = await search(knowledgeBaseId, {
				query: trimmed,
				top_k: topK,
			});
			setResults(res.results);
		} catch (e) {
			setError((e as Error).message || String(e));
			setResults(null);
		} finally {
			setLoading(false);
		}
	};

	return (
		<Sheet open={open} onOpenChange={onOpenChange}>
			<SheetContent className="flex w-full flex-col gap-y-4 p-4 sm:!max-w-[820px]">
				<SheetHeader className="px-0">
					<SheetTitle className="flex items-center gap-x-2">
						<FlaskConical className="size-4" />
						{t('knowledge.test.title')}
					</SheetTitle>
					<SheetDescription className="truncate">
						{t('knowledge.test.description', { name: knowledgeBaseName })}
					</SheetDescription>
				</SheetHeader>

				<div className="flex flex-col gap-y-3 px-0">
					<div className="flex flex-col gap-y-1.5">
						<Label htmlFor="kb-test-query" className="text-xs">
							{t('knowledge.test.queryLabel')}
						</Label>
						<Textarea
							id="kb-test-query"
							value={query}
							onChange={(e) => setQuery(e.target.value)}
							placeholder={t('knowledge.test.queryPlaceholder')}
							rows={3}
							disabled={loading}
						/>
					</div>
					<div className="flex items-center gap-x-3">
						<div className="flex items-center gap-x-2">
							<Label htmlFor="kb-test-topk" className="text-xs">
								{t('knowledge.test.topKLabel')}
							</Label>
							<Input
								id="kb-test-topk"
								type="number"
								min={1}
								max={50}
								value={topK}
								onChange={(e) =>
									setTopK(Math.max(1, Math.min(50, Number(e.target.value) || 1)))
								}
								className="w-20"
								disabled={loading}
							/>
						</div>
						<Button
							className="ml-auto"
							size="sm"
							onClick={handleSearch}
							disabled={loading || !query.trim()}
						>
							{loading ? (
								<Loader2 className="size-3.5 animate-spin" />
							) : (
								<Search className="size-3.5" />
							)}
							{t('knowledge.test.searchButton')}
						</Button>
					</div>
					{error && <p className="text-destructive text-sm">{error}</p>}
				</div>

				<div className="flex-1 overflow-y-auto px-0">
					{results === null ? null : results.length === 0 ? (
						<Empty className="border-none py-4">
							<EmptyHeader>
								<EmptyMedia variant="icon">
									<Search />
								</EmptyMedia>
								<EmptyTitle>{t('knowledge.test.emptyTitle')}</EmptyTitle>
								<EmptyDescription>
									{t('knowledge.test.emptyDescription')}
								</EmptyDescription>
							</EmptyHeader>
						</Empty>
					) : (
						<div className="flex flex-col gap-y-3">
							{results.map((hit, idx) => (
								<ResultCard
									key={`${hit.document_id}-${idx}`}
									hit={hit}
									index={idx}
								/>
							))}
						</div>
					)}
				</div>

				<SheetFooter className="px-0">
					<Button variant="ghost" onClick={() => onOpenChange(false)}>
						{t('common.close')}
					</Button>
				</SheetFooter>
			</SheetContent>
		</Sheet>
	);
}

/** One hit, collapsed to its rank / score / source until opened. Chunks run
 *  long, and a wall of them buries the ranking the test is meant to show. */
function ResultCard({ hit, index }: { hit: VectorSearchResult; index: number }) {
	const { t } = useTranslation();
	const [open, setOpen] = useState(false);
	const text =
		hit.chunk.content && typeof hit.chunk.content === 'object' && 'text' in hit.chunk.content
			? String(hit.chunk.content.text ?? '')
			: JSON.stringify(hit.chunk.content);
	// Written by the structured parser; documents indexed before it, and formats
	// with no headings to find, carry nothing here.
	const rawPath = hit.chunk.metadata?.heading_path;
	const headingPath = Array.isArray(rawPath)
		? rawPath.filter((part): part is string => typeof part === 'string').join(' / ')
		: '';

	return (
		<div className="bg-card flex flex-col gap-y-2 rounded-md border">
			<button
				type="button"
				onClick={() => setOpen((v) => !v)}
				aria-expanded={open}
				className="hover:bg-muted/50 flex items-center gap-x-2 rounded-md p-3 text-left transition-colors"
			>
				<ChevronRight
					className={cn('size-3.5 shrink-0 transition-transform', open && 'rotate-90')}
				/>
				<Badge variant="secondary" className="font-mono">
					#{index + 1}
				</Badge>
				<Badge variant="outline" className="font-mono">
					{t('knowledge.test.score')}: {hit.score.toFixed(4)}
				</Badge>
				{headingPath && (
					<span className="min-w-0 truncate text-xs font-medium" title={headingPath}>
						{headingPath}
					</span>
				)}
				<span
					className="text-muted-foreground ml-auto min-w-0 truncate text-xs"
					title={hit.chunk.source}
				>
					{hit.chunk.source}
				</span>
			</button>
			{open && (
				<div className="flex flex-col gap-y-2 px-3 pb-3">
					<p className="max-h-[45vh] overflow-y-auto text-sm break-words whitespace-pre-wrap">
						{text}
					</p>
					<div className="text-muted-foreground text-xs">
						{t('knowledge.test.chunkPosition', {
							index: hit.chunk.chunk_index + 1,
							total: hit.chunk.total_chunks,
						})}
					</div>
				</div>
			)}
		</div>
	);
}
