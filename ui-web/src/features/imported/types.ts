/* What the imported domain asks of the gateway: one package up, and the
   conversation it became (or the one it already was). */

import type { ResultOf } from '../../rpc/generated'

export type ImportedUpload = ResultOf<'imported.upload'>

export interface ImportedSource {
  upload(name: string, contentB64: string, force: boolean): Promise<ImportedUpload>
}
