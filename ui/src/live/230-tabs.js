/* ── subagents: what this conversation handed off ──────────────────────
   Scoped to the open session on every call, so background work from another
   conversation can never surface here. */
/* The rows and the records, and only those: which of them is on screen, when
   to ask again and whether the answer is worth a repaint are all about what
   is drawn, and they live with the renderer -- the subagents island
   (ui/src/features/subagents/).

   A server without the surface answers -32601 forever otherwise, and the panel
   would sit empty with no way to tell an empty list from a missing feature --
   so an absent surface answers with no rows rather than an error, and
   `absent` is what the island's empty state reads to tell the two apart. */
let agentsWatch = null;
DS.agents = {
  /* Filtered on whether the agent can be dispatched, not on where it came
     from. It filtered `vendored` -- which is true of every agent that ships
     WITH raven -- so Raven-Code, Raven-PPT and Raven-Research were absent from
     the roster while `enabled: false` rows (an uninstalled acp preset, a
     disabled cli) were listed as if they were available. An agent that is off
     but has instances still gets a group: `orderAgentGroups` unions these names
     with the ones on the instance rows, so its history stays reachable. */
  roster: () => rpc.call('subagents.list', { probe: false })
    .then(r => (r.rows || []).filter(row => row.enabled)),
  list: (sessionId) => {
    if (!rpcHas('subagent')) return Promise.resolve([]);
    return rpc.call('subagent.list', { session_id: sessionId })
      .then((r) => (r && r.items) || [])
      /* Absent surface -> no rows (the island words that empty state); a call
         that merely failed rethrows, so the page keeps what it last drew --
         a dropped socket must not repaint a live run as "no delegated work". */
      .catch((e) => { if (rpcGone('subagent', e)) return []; throw e; });
  },
  /* A call is addressed by conversation and call, not by call alone: its
     record lives inside that conversation's own directory. A dag node is
     addressed by (run, node), reconciled server-side against the registry. */
  context: (id) => rpc.call('subagent.context', { id, session_id: sessionCurrent() }),
  node: (runId, node) => rpc.call('dag.node', { run_id: runId, node, session_key: sessionCurrent() })
    .then((r) => (r && r.node) || {}),
  absent: () => !rpcHas('subagent'),
  /* The stateful handles. Scoped to the open session like everything else here,
     and answering with no rows on an absent surface for the same reason `list`
     does -- a panel that cannot tell "none" from "not supported" makes the
     reader guess. */
  instances: (sessionId) => {
    if (!rpcHas('subagents')) return Promise.resolve([]);
    return rpc.call('subagents.instances', { session_key: sessionId })
      .then((r) => (r && r.instances) || [])
      .catch((e) => { if (rpcGone('subagents', e)) return []; throw e; });
  },
  /* Addressed by (agent, handle) inside the open session: a handle is unique
     per agent, not globally, so both halves travel. */
  instanceHistory: (agent, handle) =>
    rpc.call('subagents.instance.history', { session_key: sessionCurrent(), agent, handle }).then((r) => r || {}),
  instanceForget: (agent, handle) =>
    rpc.call('subagents.instance.forget', { session_key: sessionCurrent(), agent, handle }).then(() => undefined),
  /* A turn addressed to one instance rather than to the conversation: the same
     `turn.send` the composer uses, with a `target`. An instance's turn runs on
     its own lane, so it is concurrent with the main agent's and with every other
     instance's, and is refused only by *that* instance still answering. */
  instanceSend: (agent, handle, text) =>
    rpc.call('turn.send', { session_key: sessionCurrent(), content: text, target: { agent, handle } })
      .then(() => undefined),
  instanceCreate: (agent, sessionKey) =>
    rpc.call('subagents.instance.create', { agent, session_key: sessionKey })
      .then((r) => r && r.instance),
  /* The heartbeat, forwarded rather than acted on: a run in flight has to
     move on screen without being reopened, and every judgement about what
     that takes belongs to the island that is drawing it. */
  watch: (fn) => { agentsWatch = fn; },
  /* Draws a delegated run's record with the transcript's own renderer -- the
     transcript island, which owns the incremental bookkeeping too (what is
     already drawn, the held-back streaming answer, the working glyph, the
     scroll). A member here rather than its own binding, because a painter is
     only ever wanted for a record, and this is the source the records come
     from. */
  stagePaint: (box, r, opts) => RavenIslands.transcript.agentStage(box, r, opts),
};

if (new URLSearchParams(location.search).get('desk-demo') === '1') {
  const now = Date.now();
  const liveRoster = DS.agents.roster;
  const demoInstances = [
    { sessionKey: 'desk-demo', agent: 'research-raven', handle: 'market-map-a19f', kind: 'playbook', status: 'running', runId: '市场调研', nodeId: '竞品功能调研', createdAtMs: now - 420000, updatedAtMs: now, resumable: true },
    { sessionKey: 'desk-demo', agent: 'research-raven', handle: 'model-permissions-7c2a', kind: 'playbook', status: 'completed', runId: 'UI 能力核验', nodeId: '整理权限模型差异', createdAtMs: now - 830000, updatedAtMs: now - 220000, resumable: true },
    { sessionKey: 'desk-demo', agent: 'coding-raven', handle: 'instance-sync-coder12', kind: 'dag', status: 'running', runId: '子智能体列表改造', nodeId: '修复实例状态同步', createdAtMs: now - 190000, updatedAtMs: now, resumable: true },
    { sessionKey: 'desk-demo', agent: 'coding-raven', handle: 'history-render-9d0e', kind: 'playbook', status: 'failed', runId: 'UI 回归', nodeId: '验证历史消息渲染', createdAtMs: now - 620000, updatedAtMs: now - 480000, resumable: true },
    { sessionKey: 'desk-demo', agent: 'coding-raven', handle: 'legacy-build-31ab', kind: 'spawn', status: 'completed', nodeId: '旧版构建迁移', createdAtMs: now - 940000, updatedAtMs: now - 720000, resumable: false },
    { sessionKey: 'desk-demo', agent: 'content-raven', handle: 'release-notes-18ca', kind: 'dag', status: 'completed', runId: '发布流程', nodeId: '整理发布说明', createdAtMs: now - 380000, updatedAtMs: now - 140000, resumable: true },
    { sessionKey: 'desk-demo', agent: 'review-raven', handle: 'interaction-coverage-612e', kind: 'playbook', status: 'idle', runId: '交互验收', nodeId: '检查交互状态覆盖', createdAtMs: now - 50000, updatedAtMs: now - 50000, resumable: true },
  ];
  const demoHistory = new Map(demoInstances.map((row) => [row.handle, [
    { call_id: `${row.handle}-u`, role: 'user', content: `请执行「${row.nodeId}」，完成后给出可验证的结果。`, at_ms: row.createdAtMs },
    { call_id: `${row.handle}-a`, role: 'assistant', content: row.status === 'failed'
      ? '检查过程中发现历史记录的消息结构与渲染器不一致，需要修正后重试。'
      : '已读取任务上下文并完成第一轮处理，正在整理关键结果。', at_ms: row.updatedAtMs, live: row.status === 'running' },
  ]]));
  let bindDemo = null;
  const bindDemoAgents = () => {
    if (bindDemo) return bindDemo;
    bindDemo = liveRoster().then((rows) => {
      const names = (rows || []).map((row) => row.name).filter(Boolean);
      if (!names.length) return;
      const groups = [...new Set(demoInstances.map((row) => row.agent))];
      const mapped = new Map(groups.map((name, index) => [name, names[index % names.length]]));
      demoInstances.forEach((row) => { row.agent = mapped.get(row.agent) || row.agent; });
    }).catch(() => {});
    return bindDemo;
  };
  DS.agents.instances = async () => {
    await bindDemoAgents();
    return demoInstances.slice();
  };
  DS.agents.instanceHistory = async (_agent, handle) => ({ turns: (demoHistory.get(handle) || []).slice() });
  DS.agents.instanceForget = async (agent, handle) => {
    const at = demoInstances.findIndex((row) => row.agent === agent && row.handle === handle);
    if (at >= 0) demoInstances.splice(at, 1);
  };
  DS.agents.instanceSend = async (agent, handle, text) => {
    const row = demoInstances.find((item) => item.agent === agent && item.handle === handle);
    if (!row) throw new Error('Instance not found');
    row.status = 'running'; row.updatedAtMs = Date.now();
    const turns = demoHistory.get(handle) || [];
    turns.push({ call_id: `${handle}-${Date.now()}-u`, role: 'user', content: text, at_ms: Date.now() });
    demoHistory.set(handle, turns);
    RavenIslands.subagents.directEvent({ agent, handle }, 'message.start', { content: text });
    setTimeout(() => {
      turns.push({ call_id: `${handle}-${Date.now()}-a`, role: 'assistant', content: `已收到并完成：${text}`, at_ms: Date.now() });
      row.status = 'completed'; row.updatedAtMs = Date.now();
      RavenIslands.subagents.directEvent({ agent, handle }, 'message.complete', {});
    }, 1200);
  };
}
setInterval(() => { if (agentsWatch) agentsWatch(); }, 2000);
