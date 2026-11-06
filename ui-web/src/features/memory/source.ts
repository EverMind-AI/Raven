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
  /* The switch is a settings write, not a memory.* verb: it changes the same
     file the settings section owns, and the server admits the key there. */
  setBackend: (backend) => gateway().call('settings.set', {
    key: 'memory.backend',
    value: backend,
  } as ParamsOf<'settings.set'>),
  setCloudKey: (key) => gateway().call('settings.set', {
    key: 'plugins.config.everos-cloud-memory.api_key',
    value: key,
  } as ParamsOf<'settings.set'>),
  /* The same probe the settings card uses: it builds the backend from the
     slice just written and calls health(), so a rejected key answers here
     rather than in a memory read the section would have to interpret. */
  probeCloud: () => gateway().call('settings.everosCloud', {}),
  /* Asked before the write: a turn in flight turns the switch into a question
     the reader answers, rather than a spinner that lasts as long as the turn. */
  switchStatus: () => gateway().call('memory.switchStatus', {}),
  /* A live reference swap inside the running loop -- no generation reload, so
     the document's socket stays up. A failed call is a real failure now. */
  applyRuntime: async () => {
    try {
      return await gateway().call('memory.applyRuntime', {})
    } catch (e) {
      return { ok: false, detail: (e as Error).message || String(e) }
    }
  },
  startLocal: () => gateway().call('memory.startLocal', {}),
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
