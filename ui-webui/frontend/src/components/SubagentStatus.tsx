import type { RavenSubagentProbe } from '@/api';
import { Button } from '@/components/ui/button';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import Refresh from '~icons/solar/refresh-linear';

/** Status colour and label live together in one map so the dot in the sidebar
 *  and the line in the pane can never disagree about what a status means. */
const DOT: Record<string, string> = {
	ready: 'bg-emerald-500',
	attention: 'bg-amber-500',
	missing: 'bg-destructive',
	unknown: 'bg-muted-foreground',
};

const LABEL_KEY: Record<string, string> = {
	ready: 'subagent-sidebar.statusReady',
	attention: 'subagent-sidebar.statusAttention',
	missing: 'subagent-sidebar.statusMissing',
	unknown: 'subagent-sidebar.statusUnknown',
};

/** Colour and label for one probe, with a remembered test failure outranking a
 *  green probe: "which found it and it still cannot authenticate" is exactly the
 *  case the free probe gets wrong, and it is the whole reason verdicts persist.
 *  A `missing` or `unknown` probe still wins, because "not installed" is the more
 *  actionable fact and a verdict from before an uninstall says nothing useful. */
function effective(probe: RavenSubagentProbe): { dot: string; labelKey: string } {
	if (probe.status === 'missing' || probe.status === 'unknown') {
		return { dot: DOT[probe.status], labelKey: LABEL_KEY[probe.status] };
	}
	if (probe.lastTest && !probe.lastTest.ok) {
		return { dot: DOT.missing, labelKey: 'subagent-sidebar.statusTestFailed' };
	}
	return {
		dot: DOT[probe.status] ?? DOT.unknown,
		labelKey: LABEL_KEY[probe.status] ?? LABEL_KEY.unknown,
	};
}

/** Coarse age for a remembered verdict, so it is never read as fresh. The units
 *  are compact and language-neutral; the sentence around them is translated. */
function ageText(testedAtMs: number): string {
	const mins = Math.max(0, Math.round((Date.now() - testedAtMs) / 60000));
	if (mins < 1) return '<1m';
	if (mins < 60) return `${mins}m`;
	const hours = Math.round(mins / 60);
	if (hours < 24) return `${hours}h`;
	return `${Math.round(hours / 24)}d`;
}

/** The sidebar affordance: a dot, with the full verdict on hover. Renders
 *  nothing until the probe lands, so rows never jump. */
export function SubagentStatusDot({ probe }: { probe?: RavenSubagentProbe }) {
	const { t } = useTranslation();
	if (!probe) return null;
	const { dot, labelKey } = effective(probe);
	const label = t(labelKey);
	return (
		<span
			role="img"
			className={cn('size-1.5 shrink-0 rounded-full', dot)}
			title={`${label} - ${probe.detail}`}
			aria-label={label}
		/>
	);
}

interface SubagentStatusLineProps {
	probe: RavenSubagentProbe | null;
	probing: boolean;
	/** Whether a probe fetch has ever succeeded. `false` with no probe and no
	 *  fetch in flight means the first fetch failed, not that one is pending. */
	probesLoaded: boolean;
	onRefresh: () => void;
	/** cli only: the probe checks the command's executable, not the agent. */
	showCliHint?: boolean;
}

/** The pane affordance: the verdict in words, plus what was checked. */
export function SubagentStatusLine({
	probe,
	probing,
	probesLoaded,
	onRefresh,
	showCliHint,
}: SubagentStatusLineProps) {
	const { t } = useTranslation();
	const { dot, labelKey } = probe ? effective(probe) : { dot: DOT.unknown, labelKey: '' };
	const statusLabel = probe
		? t(labelKey)
		: probesLoaded || probing
			? t('subagent-sidebar.statusChecking')
			: t('subagent-sidebar.statusUnavailable');
	return (
		<div className="rounded-lg border px-3 py-2">
			<div className="flex items-center gap-x-2">
				<span className={cn('size-1.5 shrink-0 rounded-full', dot)} />
				<span className="text-sm font-medium">{statusLabel}</span>
				<Button
					size="icon-sm"
					variant="ghost"
					className="ml-auto"
					onClick={onRefresh}
					disabled={probing}
					title={t('subagent-sidebar.statusRefresh')}
				>
					<Refresh className={cn('size-3.5', probing && 'animate-spin')} />
				</Button>
			</div>
			{probe && <p className="text-muted-foreground mt-1 text-[11px]">{probe.detail}</p>}
			{probe?.lastTest && (
				<p className="text-muted-foreground mt-1 text-[11px]">
					{probe.lastTest.ok
						? t('subagent-sidebar.testPassed')
						: t('subagent-sidebar.testFailed')}
					{' - '}
					{t('subagent-sidebar.testedAgo', { age: ageText(probe.lastTest.testedAtMs) })}
					{!probe.lastTest.ok && `: ${probe.lastTest.detail}`}
				</p>
			)}
			{showCliHint && (
				<p className="text-muted-foreground mt-1 text-[11px]">
					{t('subagent-sidebar.statusCliHint')}
				</p>
			)}
		</div>
	);
}
