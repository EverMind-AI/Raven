/* Hosted-terminal RPC source. The session id scopes the island lifecycle;
   the host resolves its working tree from the open session's workspace. */
DS.terminal = {
  list: () => {
    if (!wsRoot) return Promise.resolve({
      terminals: [],
      truncated: false,
      hostScope: { hostIds: [], omittedHostIds: [] },
      topologyRevisions: {},
    });
    return rpc.call('terminal.list', {
      worktree_id: wsRoot,
      limit: 1000,
      include_visual_layouts: true,
    });
  },
};
