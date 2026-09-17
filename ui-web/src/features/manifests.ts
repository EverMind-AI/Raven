/* Every domain, declared once.
 *
 * What a page IS -- its section id, the box its island fills, the rail button
 * it lights, its place in the Escape chain -- is one table in state/pages.ts,
 * because every table that names a page is the page's own layers reading it.
 * What a domain DOES is here: which page it owns, which seam keys it answers,
 * and the root src/main.tsx mounts for it. Nine of those roots used to be nine
 * copies of `const host = getElementById(...); if (host) createRoot(host)` in
 * main.tsx, and the guard's only reachable effect was an island that silently
 * failed to mount -- an empty page, no error (src/main.tsx says what replaced
 * it).
 *
 * The assembly point rather than a domain: every `features/<domain>/manifest.ts`
 * declares into this file, and no domain may read it -- an import the other way
 * would put every island in every island's closure, which is the shape the
 * island bag had (scripts/gates/import-direction.test.mjs ranks it above the
 * domains for exactly that reason). The per-domain files take `DomainManifest`
 * from here as a type, which erases, so the cycle a reader sees is not one the
 * bundle has.
 *
 * Two fields the plan for this step named are not here yet, and both would be
 * declarations of something untrue today: an `i18n` namespace (measured: no
 * domain's keys live under a single one, and nine share their commonest with
 * another domain -- the rename step is what can declare it) and an
 * `onLangChange` (two domains have an imperative repaint left after the
 * language became a subscription, and state/lang/effects.ts names them by
 * hand because state/ may not import this file).
 */

import { manifest as browser } from './browser/manifest'
import { manifest as composer } from './composer/manifest'
import { manifest as connections } from './connections/manifest'
import { manifest as cron } from './cron/manifest'
import { manifest as dag } from './dag/manifest'
import { manifest as desk } from './desk/manifest'
import { manifest as extAgents } from './extAgents/manifest'
import { manifest as installed } from './installed/manifest'
import { manifest as knowledge } from './knowledge/manifest'
import { manifest as memory } from './memory/manifest'
import { manifest as model } from './model/manifest'
import { manifest as onboard } from './onboard/manifest'
import { manifest as playbooks } from './playbooks/manifest'
import { manifest as plugins } from './plugins/manifest'
import { manifest as rail } from './rail/manifest'
import { manifest as settings } from './settings/manifest'
import { manifest as skills } from './skills/manifest'
import { manifest as subagents } from './subagents/manifest'
import { manifest as transcript } from './transcript/manifest'
import { manifest as workspace } from './workspace/manifest'

import type { PageId } from '../state/pages'
import type { Sources } from '../state/sources'
import type { JSX } from 'react'

/** What one domain declares about itself. */
export interface DomainManifest {
  /** The directory name, which is the domain's name everywhere else too. */
  readonly domain: string
  /** The module page it owns, where it owns one (state/pages.ts). */
  readonly page?: PageId
  /** The seam keys it answers, installed by src/app/install.ts. */
  readonly sources: readonly (keyof Sources)[]
  /** Its root component, for the islands the page mounts into a rendered box.
   *  Two of them render nothing until something opens them, which is why the
   *  return is nullable. */
  readonly root?: () => JSX.Element | null
  /** The box that root goes into, when it is not the page's own body. */
  readonly host?: string
}

/** The twenty, alphabetically: nothing reads them in an order. */
export const MANIFESTS: readonly DomainManifest[] = [
  browser,
  composer,
  connections,
  cron,
  dag,
  desk,
  extAgents,
  installed,
  knowledge,
  memory,
  model,
  onboard,
  playbooks,
  plugins,
  rail,
  settings,
  skills,
  subagents,
  transcript,
  workspace,
]
