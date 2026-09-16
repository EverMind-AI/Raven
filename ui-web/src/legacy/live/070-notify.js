/* ---- notifications ------------------------------------------------ */
/* Every frame the gateway pushes is routed by the session pipeline
   (ui-web/src/state/session/pipeline.ts): the turn stream by the subscription
   it names, and the five side-channel requests by the conversation whose turn
   is blocked on the answer. */

import { installPipeline } from '../../state/session/pipeline'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  installPipeline();
}
