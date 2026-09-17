/* The DataSource seam: one module, one domain per member, each typed.
 *
 * A page's renderer lives once and reads its data only through a source. The
 * page installs one per domain synchronously before the first data-driven
 * paint (src/state/install.ts), and which transport answers behind it is the
 * URL's decision rather than the renderer's (src/state/transport.ts). No flags,
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
import type { ConnSource } from '../features/connections/types'
import type { CronSource } from '../features/cron/types'
import type { KnowledgeSource } from '../features/knowledge/types'
import type { MemorySource } from '../features/memory/types'
import type { ModelSource } from '../features/model/types'
import type { OnboardSource } from '../features/onboard/types'
import type { PlaybooksSource } from '../features/playbooks/types'
import type { PluginsSource } from '../features/plugins/types'
import type { RailSource } from '../features/rail/types'
import type { SettingsSource } from '../features/settings/types'
import type { SkillsSource } from '../features/skills/types'
import type { AgentsSource } from '../features/subagents/types'
import type { ArtifactsSource, TranscriptSource } from '../features/transcript/types'
import type { WorkspaceSource } from '../features/workspace/types'
import type { XaSource } from '../features/xa/types'
import type { ProseSource } from '../lib/prose'
import type { BannerSource } from './banner'
import type { TierSource } from './tier'

/* Whether the extensions list has been read, and reading it. Declared here
   rather than in a feature's types because no island has this domain: the
   capabilities page is chrome, and its opener is the only reader
   (src/features/plugins/nav.ts). */
export interface CapabilitiesSource {
  loaded(): boolean
  load(): Promise<boolean>
}

export interface Sources {
  agents: AgentsSource
  artifacts: ArtifactsSource
  banner: BannerSource
  browser: BrowserSource
  capabilities: CapabilitiesSource
  composer: ComposerSource
  conn: ConnSource
  cron: CronSource
  knowledge: KnowledgeSource
  memory: MemorySource
  model: ModelSource
  onboard: OnboardSource
  playbooks: PlaybooksSource
  plugins: PluginsSource
  prose: ProseSource
  sessions: RailSource
  settings: SettingsSource
  skills: SkillsSource
  tier: TierSource
  transcript: TranscriptSource
  workspace: WorkspaceSource
  xa: XaSource
}

export const sources: Partial<Sources> = {}

export function setSources(patch: Partial<Sources>): void {
  Object.assign(sources, patch)
}

/* One domain's source, or a loud failure. An island runs inside the assembled
   page or inside a case that installed what it reads, never standalone, and a
   silent undefined would just move the failure downstream. */
export function ds<S>(domain: string): S {
  const source = sources[domain as keyof Sources] as S | undefined
  if (!source) throw new Error(`DS.${domain} is not installed`)
  return source
}

/* Back to the state a fresh page starts in. For tests: a source one case
   installed must not be visible to the next. */
export function resetSources(): void {
  for (const domain of Object.keys(sources)) delete sources[domain as keyof Sources]
}
