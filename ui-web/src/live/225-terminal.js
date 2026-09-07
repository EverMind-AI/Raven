/* Hosted-terminal RPC source. The first list stays unscoped and filters by the
   session cwd; only a returned worktreeId is trusted for later exact lists. */
let terminalScopePath = '';
let terminalScopeId = '';
const terminalSubscriptions = new Map();

const terminalEmptyList = () => ({
  terminals: [],
  truncated: false,
  hostScope: { hostIds: [], omittedHostIds: [] },
  topologyRevisions: {},
});

const terminalAttachIdentities = (result, terminals, agents) => {
  const byHandle = new Map();
  (agents || []).forEach((agent) => {
    if (agent.binding && agent.binding.handle) byHandle.set(agent.binding.handle, agent);
  });
  return {
    ...result,
    terminals: terminals.map((terminal) => {
      const agent = byHandle.get(terminal.handle);
      if (!agent) return terminal;
      return {
        ...terminal,
        identity: {
          agentName: agent.agentName,
          brand: agent.brand,
          bindingGeneration: agent.bindingGeneration,
        },
      };
    }),
  };
};

DS.terminal = {
  list: async () => {
    if (!wsRoot) return terminalEmptyList();
    if (terminalScopePath !== wsRoot) {
      terminalScopePath = wsRoot;
      terminalScopeId = '';
    }
    const [result, identities] = await Promise.all([
      rpc.call('terminal.list', {
        ...(terminalScopeId ? { worktree_id: terminalScopeId } : {}),
        limit: 1000,
        include_visual_layouts: true,
      }),
      rpc.call('agents.list', {}).catch(() => ({ agents: [] })),
    ]);
    const terminals = terminalScopeId
      ? (result.terminals || [])
      : (result.terminals || []).filter((row) => row.worktreePath === wsRoot);
    if (terminals.length) terminalScopeId = terminals[0].worktreeId;
    return terminalAttachIdentities(result, terminals, identities.agents);
  },
  input: (params) => rpc.call('terminal.input', params),
  resize: (params) => rpc.call('terminal.resize', params),
  subscribe: async (params) => {
    const result = await rpc.call('terminal.subscribe', params);
    const subscription = result.subscription || {};
    if (params.enabled === false) terminalSubscriptions.delete(params.handle);
    else if (subscription.subscription_id) terminalSubscriptions.set(params.handle, subscription.subscription_id);
    return result;
  },
  acceptEvent: (params) => {
    if (![...terminalSubscriptions.values()].includes(params.subscription_id)) return false;
    if (DS.terminal.onEvent) DS.terminal.onEvent(params.event || {});
    return true;
  },
  onEvent: null,
  onOutput: null,
};

const previousBinarySink = rpc.binary;
rpc.binary = (buf) => {
  const u8 = new Uint8Array(buf);
  if (u8.length < 8 || u8[0] !== 0x52 || u8[1] !== 0x56 || u8[2] !== 0x54 || u8[3] !== 0x31) {
    if (previousBinarySink) previousBinarySink(buf);
    return;
  }
  const headerLength = new DataView(buf).getUint32(4);
  if (headerLength > 4096 || headerLength > u8.length - 8) return;
  let header;
  try { header = JSON.parse(new TextDecoder().decode(u8.subarray(8, 8 + headerLength))); } catch { return; }
  if (!header.handle || !Number.isSafeInteger(header.seq)) return;
  if (DS.terminal.onOutput) {
    DS.terminal.onOutput({
      handle: header.handle,
      seq: header.seq,
      replay: header.replay === true,
      data: u8.subarray(8 + headerLength),
    });
  }
};
