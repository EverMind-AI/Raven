/* Hosted-terminal fixture source for the static design canvas. */
DS.terminal ??= {
  list: async () => ({
    terminals: [],
    truncated: false,
    hostScope: { hostIds: ['local'], omittedHostIds: [] },
    topologyRevisions: {},
  }),
};
