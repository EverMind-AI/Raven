import { toolLabelClass } from './_shared';
import type { ToolRenderer, ToolCallWithResult } from './types';
import { DeliveredFileRow } from '@/components/delivery/DeliveredFileRow';
import { downloadAllDeliverables, readManifest } from '@/components/delivery/deriveDeliverables';
import { Button } from '@/components/ui/button';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import Package from '~icons/solar/box-bold-duotone';

/**
 * Inline, always-open card for the ``DeliverFiles`` tool call.
 *
 * Renders the same header + manifest body as the in-collapsible
 * renderer, but without the surrounding trigger row / Collapsible
 * wrapper — used by ``MessageBubble`` when it extracts a deliverable
 * call out of the tool-chain group summary so the user immediately
 * sees the file list (they otherwise have to expand the fold to find
 * it). Returns the rendered manifest, or ``undefined`` when there is
 * no manifest yet (still streaming / no result yet / a different
 * error state), letting the caller render a fallback.
 */
export function DeliverFilesInlineCard({ pair }: { pair: ToolCallWithResult }) {
	const { t } = useTranslation();
	const manifest = readManifest(pair.result?.metadata);
	if (!manifest || manifest.files.length === 0) return undefined;
	return (
		<div className="flex flex-col gap-2 rounded-md border bg-background p-3">
			<div className="flex items-center gap-2">
				<Package className="size-3.5 shrink-0 text-foreground/80" />
				<span className={toolLabelClass}>
					{t('tool.deliverFilesTitle', { count: manifest.files.length })}
				</span>
				{pair.result?.state === 'running' && (
					<span className="ml-auto text-[10px] uppercase tracking-wider text-muted-foreground">
						{t('common.running')}
					</span>
				)}
			</div>
			{manifest.message && (
				<p className="text-xs text-muted-foreground">{manifest.message}</p>
			)}
			<div className={cn('flex flex-col gap-2')}>
				{manifest.files.map((file) => (
					<DeliveredFileRow key={file.path} file={file} />
				))}
			</div>
			{manifest.files.length > 1 && (
				<Button
					variant="outline"
					size="sm"
					className="w-fit"
					onClick={() => downloadAllDeliverables(manifest.files)}
				>
					{t('tool.deliverFilesDownloadAll')}
				</Button>
			)}
			{manifest.invalid.length > 0 && (
				<p className="text-xs text-muted-foreground">
					{t('tool.deliverFilesInvalid', {
						list: manifest.invalid.map((i) => `${i.path} (${i.reason})`).join(', '),
					})}
				</p>
			)}
		</div>
	);
}

export const DeliverFilesRenderer: ToolRenderer = {
	getDisplayName: (_call, t) => t('tool.deliverFiles'),

	renderHeader: (pair, t) => {
		const manifest = readManifest(pair.result?.metadata);
		const count = manifest?.files.length ?? 0;
		return (
			<span className={toolLabelClass}>
				{count > 0 ? t('tool.deliverFilesTitle', { count }) : t('tool.deliverFiles')}
			</span>
		);
	},

	renderBody: (pair, t) => {
		const manifest = readManifest(pair.result?.metadata);
		// No manifest (e.g. before the call runs, or metadata absent) →
		// undefined lets the registry fall back to the default text body.
		if (!manifest || manifest.files.length === 0) return undefined;
		return (
			<div className="flex flex-col gap-2">
				{manifest.message && (
					<p className="text-xs text-muted-foreground">{manifest.message}</p>
				)}
				{manifest.files.map((file) => (
					<DeliveredFileRow key={file.path} file={file} />
				))}
				{manifest.files.length > 1 && (
					<Button
						variant="outline"
						size="sm"
						className="w-fit"
						onClick={() => downloadAllDeliverables(manifest.files)}
					>
						{t('tool.deliverFilesDownloadAll')}
					</Button>
				)}
				{manifest.invalid.length > 0 && (
					<p className="text-xs text-muted-foreground">
						{t('tool.deliverFilesInvalid', {
							list: manifest.invalid.map((i) => `${i.path} (${i.reason})`).join(', '),
						})}
					</p>
				)}
			</div>
		);
	},
};
