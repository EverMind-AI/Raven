/* The cold-start import as the rail follows it: what the gateway answers about
   the run on disk, and the verbs the row needs to leave it, stop it, or start
   it again. The wizard's own start stays with the wizard (features/onboard). */

import type { ResultOf } from '../../rpc/generated'

export type ImportStatus = ResultOf<'import.status'>
export type ImportPhase = NonNullable<ImportStatus['phase']>
export type ImportPhases = NonNullable<ImportStatus['phases']>
export type ImportTier = 'memory_files' | 'full'

export interface ImportStarted {
  started: boolean
  total: number
  detail: string
}

export interface ImportSyncSource {
  status(): Promise<ImportStatus>
  /* The same call the wizard's sync step makes; here it restarts a run the
     status says stopped short, with the request the status carries. */
  run(platforms: string[], tier: ImportTier): Promise<ImportStarted>
  stop(): Promise<{ stopped: boolean }>
}
