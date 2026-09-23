/* -- the rail's import row: the rpc source ---------------------------------- */

import { gateway } from '../../rpc/gateway'

import type { ImportSyncSource } from './types'

export const importSyncSource: ImportSyncSource = {
  status: () => gateway().call('import.status', {}),
  run: (platforms, tier) => gateway().call('import.run', { platforms, tier }),
  stop: () => gateway().call('import.stop', {}),
}
