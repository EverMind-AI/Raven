/* Hosted-terminal fixture source for the static design canvas. */
DS.terminal ??= {
  list: async () => ({
    terminals: [],
    truncated: false,
    hostScope: { hostIds: ['local'], omittedHostIds: [] },
    topologyRevisions: {},
  }),
  input: async () => ({}),
  resize: async () => ({}),
  subscribe: async ({ handle, enabled = true }) => ({
    subscription: { handle, enabled, seq: 0, ackBytes: 65536, subscription_id: `demo-${handle}` },
  }),
  onOutput: null,
  onEvent: null,
};
