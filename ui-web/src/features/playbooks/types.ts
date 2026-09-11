/* What the playbook library page reads.
 *
 * The row and detail shapes come straight from the contract
 * (`rpc-schema/openrpc.json` -> `ui-web/src/rpc/generated.ts`), so the page cannot
 * drift from the handlers by re-declaring them here. Only the source interface
 * is local: it is the seam both the fixture source (demo shell) and the rpc
 * source (live layer) implement.
 */

import type { PlaybookDetail, PlaybookNode, PlaybookNodeShape, PlaybookParam, PlaybookRow } from '../../rpc/generated'

export type { PlaybookDetail, PlaybookNode, PlaybookNodeShape, PlaybookParam, PlaybookRow }

export interface PlaybooksSource {
  /* Every playbook in both library layers. Carries each graph's shape, because
     a card draws a diagram and one fetch per card would make opening the page
     N round trips. */
  list(): Promise<PlaybookRow[]>
  /* One playbook, whole -- the per-node config the detail panel reads. */
  get(name: string): Promise<PlaybookDetail>
}
