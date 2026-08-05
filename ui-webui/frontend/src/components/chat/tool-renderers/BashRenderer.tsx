import { getResultText, parseInput, toolLabelClass } from './_shared';
import type { ToolRenderer } from './types';

function getCommand(input: string): string {
	const { command } = parseInput(input) as { command?: string };
	return command || input;
}

export const BashRenderer: ToolRenderer = {
	getDisplayName: (_call, t) => t('tool.bash.name'),

	renderConfirmBody: (call) => {
		const { command, description } = parseInput(call.input) as {
			command?: string;
			description?: string;
		};
		return (
			<div className="w-full max-w-full overflow-hidden text-ellipsis truncate">
				<div className="text-secondary-foreground font-mono">{command}</div>
				{description && <div className="text-muted-foreground">{description}</div>}
			</div>
		);
	},

	renderHeader: (pair, t) => (
		<>
			<span className={toolLabelClass}>{t('tool.bash.name')}</span>
			{/* The command is code, so it keeps a mono font rather than the shared
			    argument style; it still brightens on hover like every other row. */}
			<span className="font-mono min-w-0 truncate transition-colors group-hover:text-foreground">
				{getCommand(pair.call.input)}
			</span>
		</>
	),

	renderBody: (pair, t) => {
		if (!pair.result) return null;

		const shellRes = getResultText(pair.result);
		return (
			<div className="flex flex-col border bg-background rounded-sm p-2 text-xs">
				<div className="text-muted-foreground">{t('tool.bash.input')}</div>
				<pre className="overflow-x-auto p-2 border rounded bg-secondary ">
					{JSON.stringify(parseInput(pair.call.input), null, 2)}
				</pre>
				<div className="text-muted-foreground mt-2">{t('tool.bash.output')}</div>
				<pre className="overflow-auto p-2 border rounded bg-secondary max-h-[200px]">
					{shellRes}
				</pre>
			</div>
		);
	},
};
