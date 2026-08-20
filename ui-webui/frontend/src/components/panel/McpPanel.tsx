import { useState } from 'react';

import type { MCPClient, RavenMcpServer } from '@/api';
import { DeleteDialog } from '@/components/dialog/DeleteDialog.tsx';
import { CreateMCPDialog } from '@/components/dialog/MCPDialog.tsx';
import { PanelEmpty } from '@/components/panel/PanelEmpty';
import { Button } from '@/components/ui/button';
import { InputGroup, InputGroupAddon, InputGroupInput } from '@/components/ui/input-group';
import { Item, ItemActions, ItemContent, ItemDescription, ItemTitle } from '@/components/ui/item';
import { Kbd, KbdGroup } from '@/components/ui/kbd';
import { useTranslation } from '@/i18n/useI18n.ts';
import PlusCircle from '~icons/solar/add-circle-bold-duotone';
import DangerTriangle from '~icons/solar/danger-triangle-bold-duotone';
import SearchX from '~icons/solar/magnifer-bold-duotone';
import Search from '~icons/solar/magnifer-bold-duotone';
import Unplug from '~icons/solar/plug-circle-bold-duotone';
import Trash from '~icons/solar/trash-bin-trash-bold-duotone';

interface McpPanelProps {
	/** Raven's configured MCP servers — the ones the chat agent can reach. */
	mcps: RavenMcpServer[];
	/** Whether the list is still loading. */
	loading?: boolean;
	/**
	 * Why the last load failed, if it did. Shown above the list, since a read
	 * that never landed leaves {@link mcps} either empty (which reads as
	 * "nothing is configured") or stale (which reads as current).
	 */
	loadError?: string | null;
	/**
	 * Whether the agent has connected its MCP servers this run. They connect
	 * lazily on the first turn that needs them, so "configured but not yet
	 * connected" is a normal state, not a failure.
	 */
	connected?: boolean;
	/**
	 * Add one or more MCP servers to raven's config.
	 *
	 * @param mcps - The MCP client configs to add.
	 */
	onAdd: (mcps: MCPClient[]) => Promise<void>;
	/**
	 * Remove an MCP server by name.
	 *
	 * @param name - The MCP server name to remove.
	 */
	onRemove: (name: string) => Promise<void>;
}

/** The dialog speaks the standard ``mcpServers`` JSON shape; raven's config
 *  uses its own field names for the same thing. */
export function toRavenMcpServer(client: MCPClient): RavenMcpServer {
	const cfg = client.mcp_config;
	if (cfg.type === 'stdio_mcp') {
		return {
			name: client.name,
			type: 'stdio',
			command: cfg.command,
			args: cfg.args ?? [],
			env: cfg.env ?? {},
		};
	}
	return {
		name: client.name,
		// The gateway auto-detects sse vs streamableHttp from the url when type
		// is omitted, and it knows the convention better than this adapter.
		url: cfg.url,
		headers: cfg.headers ?? {},
	};
}

/**
 * Pure content body for the MCP dock panel: a search box, the list of MCP
 * servers raven is configured with, and an "Add MCP" action. Holds only local
 * UI state (search text, delete confirmation target); all data arrives via
 * props so it owns no data fetching.
 *
 * Renders without its own header/border — the surrounding `Panel`
 * chrome (from `PanelDock`) provides those.
 *
 * @param mcps - The MCP servers to list.
 * @param loading - Whether the list is loading.
 * @param loadError - Why the last load failed, if it did.
 * @param connected - Whether the agent's MCP connections are up.
 * @param onAdd - Add-MCP callback.
 * @param onRemove - Remove-MCP callback.
 * @returns The MCP panel body.
 */
export function McpPanel({
	mcps,
	loading = false,
	loadError = null,
	connected = false,
	onAdd,
	onRemove,
}: McpPanelProps) {
	const { t } = useTranslation();
	const [search, setSearch] = useState('');
	const [deleteOpen, setDeleteOpen] = useState(false);
	const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

	const filtered = search
		? mcps.filter((m) => m.name.toLowerCase().includes(search.toLowerCase()))
		: mcps;

	return (
		<div className="flex flex-col flex-1 min-h-0 gap-y-2">
			<span className="text-muted-foreground text-sm">{t('panel.mcp.description')}</span>
			{/* Said whether or not the list came back empty: a stale list reads as
			    authoritative, and an empty one reads as "nothing is configured".
			    Neither is a claim a failed read supports. */}
			{loadError ? (
				<span className="text-destructive flex items-start gap-x-2 text-sm">
					<DangerTriangle className="mt-0.5 size-4 shrink-0" />
					{t('panel.mcp.loadFailed', { detail: loadError })}
				</span>
			) : null}
			<InputGroup>
				<InputGroupInput
					placeholder={t('panel.mcp.searchPlaceholder')}
					value={search}
					onChange={(e) => setSearch(e.target.value)}
				/>
				<InputGroupAddon align="inline-end">
					<Search />
				</InputGroupAddon>
			</InputGroup>

			{loading ? (
				<div className="flex flex-1 items-center justify-center">
					<p className="text-muted-foreground text-sm">{t('panel.loading')}</p>
				</div>
			) : filtered.length === 0 ? (
				<PanelEmpty
					icon={search ? SearchX : Unplug}
					title={search ? t('panel.search.emptyTitle') : t('panel.mcp.emptyTitle')}
					description={
						search
							? t('panel.search.emptyDescription', { query: search })
							: t('panel.mcp.emptyDescription')
					}
				/>
			) : (
				<div className="flex flex-col flex-1 min-h-0 overflow-y-auto gap-y-2">
					{filtered.map((mcp) => (
						<Item key={mcp.name} variant="outline">
							<ItemContent>
								<ItemTitle className="flex items-center gap-x-2">
									{/* Gold once the server's tools are registered. Muted
									    means configured but not connected yet, which is
									    normal before the first turn that needs it -- not
									    an error, so not destructive-red. Red is kept for
									    an entry the config schema rejects. */}
									<span
										className={`size-2 shrink-0 rounded-full ${
											mcp.error
												? 'bg-destructive'
												: mcp.connected
													? 'bg-gold'
													: 'bg-muted-foreground/40'
										}`}
									/>
									{mcp.name}
								</ItemTitle>
								<ItemDescription>
									{mcp.error ? (
										// Named, not swallowed: this entry is why a save
										// over the list will be refused.
										<span className="text-destructive">
											{t('panel.mcp.invalid', { detail: mcp.error })}
										</span>
									) : (
										<KbdGroup>
											<Kbd>{mcp.type === 'stdio' || mcp.command ? 'STDIO' : 'HTTP'}</Kbd>
											<Kbd>
												{mcp.connected
													? t('panel.mcp.tools', {
															count: mcp.tools?.length ?? 0,
														})
													: connected
														? t('panel.mcp.notConnected')
														: t('panel.mcp.pending')}
											</Kbd>
										</KbdGroup>
									)}
								</ItemDescription>
							</ItemContent>
							<ItemActions>
								<Button
									variant="outline"
									size="icon-sm"
									onClick={() => {
										setDeleteTarget(mcp.name);
										setDeleteOpen(true);
									}}
								>
									<Trash />
								</Button>
							</ItemActions>
						</Item>
					))}
				</div>
			)}

			<CreateMCPDialog onAdd={onAdd}>
				<Button variant="default">
					<PlusCircle />
					{t('panel.mcp.add')}
				</Button>
			</CreateMCPDialog>

			<DeleteDialog
				open={deleteOpen}
				onOpenChange={setDeleteOpen}
				title={t('common.deleteTitle', {
					entity: t('dialog-mcp-delete.entity'),
					name: deleteTarget ?? '',
				})}
				description={t('common.deleteDescription')}
				onConfirm={async () => {
					if (deleteTarget) await onRemove(deleteTarget);
				}}
			/>
		</div>
	);
}
