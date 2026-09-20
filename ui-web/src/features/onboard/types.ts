/* The first-run wizard's contracts: what its own source speaks to the gateway,
   and the shape of a step body another domain hands it. */

import type { JSX } from 'react'

export type StepId = 'model' | 'search' | 'agents' | 'sync'

/** One platform the cold-start importer knows, with what a scan found for it. */
export interface ImportPlatform {
  platform: string
  scannable: boolean
  memory_files: number
  conversations: number
  estimated_size: number
}

export interface ImportScan {
  /* False when no memory backend is configured to receive the import; the
     sync step is then not offered at all. */
  ready: boolean
  reason: string
  platforms: ImportPlatform[]
}

export interface ImportStarted {
  started: boolean
  total: number
  detail: string
}

export type ImportTier = 'memory_files' | 'full'

export interface OnboardSource {
  /* Whether a provider is configured, re-read when the wizard closes so the
     page's first-run redirects stop once the reader has set one up. */
  providerConfigured(): Promise<boolean>
  scan(): Promise<ImportScan>
  startImport(platforms: string[], tier: ImportTier): Promise<ImportStarted>
}

/* A step body the page hands the wizard (src/app/install.ts): the owning
   domain's own component over its own store, so the wizard draws the settings
   dialog's model page and the sub-agents roster rather than copies of them.
   The wizard subscribes to the store through `subscribe`, asks `loaded` before
   drawing the body and `done` for the step's forward button. */
export interface StepBody {
  Body: () => JSX.Element
  load(): Promise<void>
  subscribe(listener: () => void): () => void
  loaded(): boolean
  done(): boolean
  /* Whether a write this step made landed in a gateway that has to restart
     before it can chat on it -- a first run, where the process started with
     no model to build a loop from. The frame says so on every step after,
     since the reader would otherwise learn it from the first send failing. */
  needsRestart?(): boolean
}

/** An agent the machine has, by the id the importer knows it under. */
export interface FoundAgent {
  id: string
  name: string
}

export interface AgentsBody extends StepBody {
  found(): FoundAgent[]
}

export interface StepBodies {
  model: StepBody
  search: StepBody
  agents: AgentsBody
}
