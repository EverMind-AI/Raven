/* -- first run: the rpc source ----------------------------------------
   The onboarding island (ui-web/src/features/onboard/) draws the provider
   list and the key form; this module only knows how to speak model.* and
   setup.status over /rpc. */

import { gateway } from '../../rpc/gateway'

import type { OnboardSource } from './types'

export const onboardSource: OnboardSource = {
  options: () => gateway().call('model.options', {}),
  saveKey: (slug, api_key, api_base) => gateway().call('model.save_key', {
    slug,
    ...(api_key ? { api_key } : {}),
    ...(api_base ? { api_base } : {}),
  }),
  setModel: (value, provider) => gateway().call('config.set', { key: 'model', value, provider }),
  recheck: async () => {
    try { return (await gateway().call('setup.status', {})).provider_configured !== false }
    catch { return true }
  },
}
