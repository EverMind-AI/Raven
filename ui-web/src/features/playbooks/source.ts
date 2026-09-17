/* -- playbooks: the rpc source ----------------------------------------
   The island talks to sources.playbooks and knows nothing about transport;
   this module only knows how to speak playbooks.* over /rpc. Installing onto
   the same name is what swaps the demo library for what is really on disk.

   Both answers are unwrapped here rather than in the island: the contract
   answers an object so it can grow a field beside the list, and the page
   wants the list. */

import { gateway } from '../../rpc/gateway'

import type { PlaybooksSource } from './types'

export const playbooksSource: PlaybooksSource = {
  list: () => gateway().call('playbooks.list', {}).then((r) => (r && r.playbooks) || []),
  get: (name) => gateway().call('playbooks.get', { name }).then((r) => r && r.playbook),
  credentials: (name) => gateway().call('playbooks.credentials.get', { name }),
  setSecret: (name, param, value) => gateway().call('playbooks.credentials.set', { name, param, value }).then(() => undefined),
  clearSecret: (name, param) => gateway().call('playbooks.credentials.clear', { name, param }).then(() => undefined),
  authorize: (name, server) => gateway().call('playbooks.oauth.authorize', { name, server }),
  clearOauth: (name, server) => gateway().call('playbooks.oauth.clear', { name, server }).then(() => undefined),
}
