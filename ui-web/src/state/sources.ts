/* The DataSource seam: one module, one domain per member, each typed.
 *
 * A page's renderer lives once and reads its data only through a source. The
 * page installs one per domain synchronously before the first data-driven
 * paint (src/app/install.ts), and which transport answers behind it is the
 * URL's decision rather than the renderer's (src/rpc/chooseTransport.ts). No flags,
 * no clearing, no repaint.
 * Design: docs/specs/2026-08-19-page-datasource-seam.md
 *
 * `Partial` because which domains exist depends on the load mode. Islands ask
 * through `ds()` below, which is where a missing one becomes a loud failure
 * rather than undefined.
 *
 * The types are the islands' own, so the offline source and the live source are
 * held to one shape.
 */
import type { BrowserSource } from '../features/browser/types'
import type { ComposerSource } from '../features/composer/types'
import type { ConnectionsSource } from '../features/connections/types'
import type { CronSource } from '../features/cron/types'
import type { ExtAgentsSource } from '../features/extAgents/types'
import type { ImportSyncSource } from '../features/importSync/types'
import type { MemorySource } from '../features/memory/types'
import type { ModelSource } from '../features/model/types'
import type { OnboardSource } from '../features/onboard/types'
import type { RailSource } from '../features/rail/types'
import type { SettingsSource } from '../features/settings/types'
import type { SubagentsSource } from '../features/subagents/types'
import type { TasksSource } from '../features/tasks/types'
import type { ArtifactsSource, TranscriptSource } from '../features/transcript/types'
import type { WorkspaceSource } from '../features/workspace/types'
import type { ProseSource } from '../lib/prose'
import type { BannerSource } from './banner'
import type { TierSource } from './tier'

/* Whether the extensions list has been read, and reading it. Declared here
   rather than in a feature's types because no island has this domain: the
   capabilities page is chrome, and its opener is the only reader
*/
export interface CapabilitiesSource {
  loaded(): boolean
  load(): Promise<boolean>
}

export interface Sources {
  artifacts: ArtifactsSource
  banner: BannerSource
  browser: BrowserSource
  capabilities: CapabilitiesSource
  composer: ComposerSource
  connections: ConnectionsSource
  cron: CronSource
  extAgents: ExtAgentsSource
  importSync: ImportSyncSource
  memory: MemorySource
  model: ModelSource
  onboard: OnboardSource
  prose: ProseSource
  rail: RailSource
  settings: SettingsSource
  subagents: SubagentsSource
  tasks: TasksSource
  tier: TierSource
  transcript: TranscriptSource
  workspace: WorkspaceSource
}

export const sources: Partial<Sources> = {}

export function setSources(patch: Partial<Sources>): void {
  Object.assign(sources, patch)
}

/* One domain's source, or a loud failure. An island runs inside the assembled
   page or inside a case that installed what it reads, never standalone, and a
   silent undefined would just move the failure downstream.
   Keyed by the seam rather than by the type the caller expects: the key decides
   what comes back, so a domain renamed or misspelt is a compile error here
   instead of a throw at the first paint that reads it. */
export function ds<K extends keyof Sources>(domain: K): Sources[K] {
  const source = sources[domain]
  if (!source) throw new Error(`DS.${domain} is not installed`)
  return source
}

/* Back to the state a fresh page starts in. For tests: a source one case
   installed must not be visible to the next. */
export function resetSources(): void {
  for (const domain of Object.keys(sources)) delete sources[domain as keyof Sources]
}
