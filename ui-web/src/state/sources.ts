/* The DataSource seam: one module, one domain per member, each typed.
 *
 * A page's renderer lives once and reads its data only through a source. The
 * demo shell REGISTERS its fixture source (`sources.x ??= fixture`), the live
 * layer INSTALLS the real one (`sources.x = rpcSource`), and because both
 * layers install synchronously before the load-event paint, whichever ran last
 * is the one the first paint reads. No flags, no clearing, no repaint.
 * Design: docs/specs/2026-08-19-page-datasource-seam.md
 *
 * `Partial` because which domains exist depends on the load mode: two are the
 * offline shell's alone (`artifacts`, `composer`) and two the live layer's
 * (`knowledge`, `model`). Islands ask through `ds()` (see shell/bridge.ts),
 * which is where a missing one becomes a loud failure rather than undefined.
 *
 * The types are the islands' own, so the fixture source and the live source
 * are held to one shape. That only binds a TypeScript caller today: the two
 * layers under src/legacy/ are JavaScript and are not type-checked.
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
import type { BannerSource } from '../shell/banner'
import type { ProseSource } from '../shell/prose'
import type { TierSource } from '../shell/tier'

/* Whether the extensions list has been read, and reading it. Declared here
   rather than in a feature's types because no island has this domain: the
   capabilities page is still legacy chrome (src/legacy/demo/120-capabilities.js
   is the only reader). */
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

/* Back to the state a fresh page starts in. For tests: a source one case
   installed must not be visible to the next. */
export function resetSources(): void {
  for (const domain of Object.keys(sources)) delete sources[domain as keyof Sources]
}
