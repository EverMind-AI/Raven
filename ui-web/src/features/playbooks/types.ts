/* What the playbook library page reads.
 *
 * The row and detail shapes come straight from the contract
 * (`rpc-schema/openrpc.json` -> `ui-web/src/rpc/generated.ts`), so the page cannot
 * drift from the handlers by re-declaring them here. Only the source interface
 * is local: it is the seam both the fixture source (demo shell) and the rpc
 * source (live layer) implement.
 */

import type {
  PlaybookCredentialParam,
  PlaybookCredentialServer,
  PlaybookDetail,
  PlaybookNode,
  PlaybookNodeShape,
  PlaybookParam,
  PlaybookRow,
  PlaybooksCredentialsGetResult,
  PlaybooksOauthAuthorizeResult
} from '../../rpc/generated'

export type {
  PlaybookCredentialParam,
  PlaybookCredentialServer,
  PlaybookDetail,
  PlaybookNode,
  PlaybookNodeShape,
  PlaybookParam,
  PlaybookRow,
  PlaybooksCredentialsGetResult,
  PlaybooksOauthAuthorizeResult
}

export interface PlaybooksSource {
  /* Every playbook in both library layers. Carries each graph's shape, because
     a card draws a diagram and one fetch per card would make opening the page
     N round trips. */
  list(): Promise<PlaybookRow[]>
  /* One playbook, whole -- the per-node config the detail panel reads. */
  get(name: string): Promise<PlaybookDetail>
  /* The machine-held half of the carried servers' credentials: which secret
     params are set, which OAuth servers hold tokens. Names and booleans only.
     Optional on the seam because the fixture shell and older engines carry no
     credentials surface; the tab then says so instead of failing to draw. */
  credentials?(name: string): Promise<PlaybooksCredentialsGetResult>
  setSecret?(name: string, param: string, value: string): Promise<void>
  clearSecret?(name: string, param: string): Promise<void>
  authorize?(name: string, server: string): Promise<PlaybooksOauthAuthorizeResult>
  clearOauth?(name: string, server: string): Promise<void>
}
