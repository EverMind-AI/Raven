/* -- data & memory: the rpc source ------------------------------------
   The page renderer is the memory island (ui-web/src/features/memory/); this
   module only knows how to speak memory.* over /rpc. It is the seam's one
   memory source: a page with no engine behind it reads the same calls off the
   fixture transport (ui-web/src/rpc/fixtures/memory.ts). */

import { gateway } from '../../rpc/gateway'

import type { ParamsOf } from '../../rpc/generated'
import type { MemorySource } from './types'

export const memorySource: MemorySource = {
  stats: () => gateway().call('memory.stats', {}),
  list: (req) => gateway().call('memory.list', {
    kind: req.kind,
    page: req.page,
    page_size: req.page_size,
    /* `null`, not an omission, which is what this has always sent. The
       contract declares `q: str | None` (raven/rpc/models.py) and the
       generator renders that as `q?: string`, so the value the server takes
       cannot be spelt in the generated type. */
    q: req.q || null,
  } as ParamsOf<'memory.list'>),
}
