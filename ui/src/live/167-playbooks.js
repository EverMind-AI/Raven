/* -- playbooks: the rpc source ----------------------------------------
   The island talks to DS.playbooks and knows nothing about transport; this
   file only knows how to speak playbooks.* over /rpc. Installing onto the
   same name is what swaps the demo library for what is really on disk.

   Both answers are unwrapped here rather than in the island: the contract
   answers an object so it can grow a field beside the list, and the page
   wants the list. */
DS.playbooks = {
  list: () => rpc.call('playbooks.list', {}).then((r) => (r && r.playbooks) || []),
  get: (name) => rpc.call('playbooks.get', { name }).then((r) => r && r.playbook),
};
