/* -- workspace and the prose chips: the seam --------------------------
   The source and the path resolvers are ui-web/src/features/workspace/source.ts;
   what is left here is the install, and publishing the two pieces of panel
   chrome that module still needs from the page. */

import { proseSource, setHostPlatformReader, setShortener, workspaceSource } from '../../features/workspace/source'
import { sources } from '../../state/sources'
import { HOST_PLATFORM } from '../demo/010-kernel.js'
import { wsShortPath } from '../demo/100-workspace.js'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  setHostPlatformReader(() => HOST_PLATFORM);
  setShortener(wsShortPath);
  sources.prose = proseSource;
  sources.workspace = workspaceSource;
}
