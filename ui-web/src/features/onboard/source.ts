/* -- first run: the rpc source ----------------------------------------
   The wizard's own calls are the few no other domain owns: the first-run
   verdict, and the cold-start import behind its data-sync step. The model,
   search and agents steps read and write through the settings and extAgents
   sources, whose panes the page hands the wizard (src/app/install.ts). */

import { gateway } from '../../rpc/gateway'

import type { OnboardSource } from './types'

export const onboardSource: OnboardSource = {
  providerConfigured: async () => {
    try { return (await gateway().call('setup.status', {})).provider_configured !== false }
    catch { return true }
  },
  scan: () => gateway().call('import.scan', {}),
  startImport: (platforms, tier) => gateway().call('import.run', { platforms, tier }),
}
