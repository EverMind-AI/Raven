/* -- data & memory: the rpc source ------------------------------------
   The page renderer is the memory island (ui-web/src/features/memory/); this
   module only knows how to speak memory.* over /rpc. The live layer installs
   it onto the seam, which replaces the fixture source before the first
   paint. */

import type { MemorySource } from './types'
import type { ParamsOf } from '../../rpc/generated'

import { t } from '../../shell/bridge'
import { show as toast } from '../../shell/toast'
import { gateway } from '../../state/gateway'

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
  /* Toasted here, and still rejected as handled: the island's success
     branch closes the drawer and reloads, which must not run on a failed
     delete. */
  remove: (it) => gateway().call('memory.delete', { kind: it.kind, id: it.id })
    .then(() => toast(t('gui.mem.deleted')))
    .catch((e) => {
      toast(t('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e }))
      throw { handled: true }
    }),
}
