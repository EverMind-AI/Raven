/* -- playbooks: the rpc source ----------------------------------------
   The island talks to sources.playbooks and knows nothing about transport; this
   file only knows how to speak playbooks.* over /rpc. Installing onto the
   same name is what swaps the demo library for what is really on disk.

   Both answers are unwrapped here rather than in the island: the contract
   answers an object so it can grow a field beside the list, and the page
   wants the list. */

import { gateway } from '../../state/gateway'
import { sources } from '../../state/sources'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.playbooks = {
    list: () => gateway().call('playbooks.list', {}).then((r) => (r && r.playbooks) || []),
    get: (name) => gateway().call('playbooks.get', { name }).then((r) => r && r.playbook),
    credentials: (name) => gateway().call('playbooks.credentials.get', { name }),
    setSecret: (name, param, value) => gateway().call('playbooks.credentials.set', { name, param, value }).then(() => undefined),
    clearSecret: (name, param) => gateway().call('playbooks.credentials.clear', { name, param }).then(() => undefined),
    authorize: (name, server) => gateway().call('playbooks.oauth.authorize', { name, server }),
    clearOauth: (name, server) => gateway().call('playbooks.oauth.clear', { name, server }).then(() => undefined),
  };
}
