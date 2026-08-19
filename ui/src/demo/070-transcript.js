/* ══ transcript: three voices, three folding depths ═══════════════
   Machine work renders as quiet activity rows, never cards. A stretch of
   consecutive calls is one work segment: folded it is one phrase line;
   opened, one row per call; a call with output opens further into its
   detail block — the only place raw commands, paths and results appear.
   Folding may hide detail but never hides a failure. */
/* Every fold in the transcript changes the height of what is above the reader,
   and the browser's own scroll anchoring may pick a node BELOW the click --
   which slides the line you just clicked out from under the cursor. So pin that
   row: measure it, mutate, correct the scroll by the difference.

   The correction has to run with smooth scrolling off. The container sets
   `scroll-behavior: smooth` for the tail-follow, and under it an assignment to
   scrollTop animates -- turning the fix into a visible glide, which is the very
   thing it exists to prevent. */
function pinRow(el, mutate) {
  const sc = $('#scroll');
  const was = el.getBoundingClientRect().top;
  mutate();
  if (!sc) return;
  const keep = sc.style.scrollBehavior;
  sc.style.scrollBehavior = 'auto';
  sc.scrollTop += el.getBoundingClientRect().top - was;
  sc.style.scrollBehavior = keep;
}

const MIN_RUN_STEPS = 2;      // silent steps to merge into one segment
const DTL_MAX_LINES = 80;

const shortArg = (a, max = 40) => {
  if (!a) return '';
  const one = String(a).replace(/\s+/g, ' ').trim();
  return one.length > max ? one.slice(0, max - 1) + '…' : one;
};

/* Normalize a call's identity: unwrap the tool_call meta tool (the row must
   name the inner tool, not the wrapper), spot mcp_<server>_<tool> names. */
function actId(name, rawArgs) {
  let a = (rawArgs && typeof rawArgs === 'object') ? rawArgs : wsArgs(name, rawArgs);
  let via = false;
  if (name === 'tool_call' && typeof a.name === 'string') {
    via = true;
    name = a.name;
    a = (a.arguments && typeof a.arguments === 'object') ? a.arguments : {};
  }
  const m = MCP_RE.exec(name);
  return { name, args: a, via, srv: m ? m[1] : null };
}

/* Line icons for the activity rows. A row names its kind of work by shape
   before it is read, so a stretch of work scans without parsing every verb.
   Tools that share a kind share a glyph on purpose (all three web tools are a
   globe); ICO holds the glyphs the workspace already draws. */
const ACT_ICO = {
  doc: 'M7 3.5h7L18.5 8v10.5a2 2 0 0 1-2 2h-9a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2ZM13.5 3.5V8h4.5',
  folder: 'M4 7.5c0-1.1.9-2 2-2h3.5l2 2.5H18c1.1 0 2 .9 2 2v7c0 1.1-.9 2-2 2H6c-1.1 0-2-.9-2-2v-9.5Z',
  term: 'M3.5 5h17v14h-17zM7.5 10l2.5 2-2.5 2M12.5 14.5H16',
  globe: 'M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17ZM3.5 12h17M12 3.5c-4 4.3-4 12.7 0 17'
    + 'M12 3.5c4 4.3 4 12.7 0 17',
  find: 'M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13ZM15.4 15.4 20 20',
  pen: 'M4.5 19.5h4L19 9a2.12 2.12 0 0 0-3-3L5.5 16.5v3ZM15.5 6.5l2 2',
  star: 'M12 4l1.9 5.3L19 11l-5.1 1.7L12 18l-1.9-5.3L5 11l5.1-1.7Z',
  chat: 'M4.5 6.5a2 2 0 0 1 2-2h11a2 2 0 0 1 2 2v6.5a2 2 0 0 1-2 2H10l-4 3.5V15H6.5a2 2 0 0 1-2-2Z',
  clock: 'M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17ZM12 7.5V12l3.4 2',
  image: 'M4.5 5.5h15v13h-15zM8.6 11.2a1.55 1.55 0 1 0 0-3.1 1.55 1.55 0 0 0 0 3.1ZM6 17.5l4.5-4.5 3.5 3.5 2.5-2.5 2.5 2.5',
  sound: 'M5 10h3l4-3.5v11L8 14H5ZM15.5 9.5a4 4 0 0 1 0 5',
  video: 'M4.5 6.5h10v11h-10zM14.5 11l5-3v8l-5-3',
  ask: 'M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17ZM9.8 9.6a2.2 2.2 0 1 1 3.4 1.9c-.8.5-1.2 1-1.2 2M12 16.6h.01',
  bad: 'M12 4.5 20.5 19H3.5L12 4.5ZM12 10v3.6M12 16.4h.01',
  dot: 'M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17ZM8.5 12h7',
  chev: 'M9.5 6.5 15 12l-5.5 5.5',
  check: 'M5 12.5l4.5 4.5L19 7',
  dag: 'M7.4 12a2.2 2.2 0 1 1-4.4 0 2.2 2.2 0 0 1 4.4 0ZM21 6a2.2 2.2 0 1 1-4.4 0 2.2 2.2 0 0 1 4.4 0Z'
    + 'M21 18a2.2 2.2 0 1 1-4.4 0 2.2 2.2 0 0 1 4.4 0ZM7.3 11l9.4-4M7.3 13l9.4 4',
};
/* Every fold in the transcript turns the same chevron. */
const chev = () => ico(ACT_ICO.chev, 'cv');
function actIco(name) {
  switch (name) {
    case 'read_file': case 'read_skill': return ACT_ICO.doc;
    case 'write_file': case 'edit_file': return ACT_ICO.pen;
    case 'list_dir': return ACT_ICO.folder;
    case 'grep': case 'find': case 'tool_search': return ACT_ICO.find;
    case 'exec': return ACT_ICO.term;
    case 'web_search': case 'web_fetch': case 'deep_research': return ACT_ICO.globe;
    case 'understand_media': return ACT_ICO.doc;
    case 'image_generate': return ACT_ICO.image;
    case 'video_generate': return ACT_ICO.video;
    case 'text_to_speech': return ACT_ICO.sound;
    case 'message': return ACT_ICO.chat;
    case 'cron': return ACT_ICO.clock;
    case 'spawn': case 'use_skill': return ACT_ICO.star;
    case 'run_subagent_dag': return ACT_ICO.dag;
    case 'ask_user': return ACT_ICO.ask;
    default: return ACT_ICO.dot;
  }
}

/* The file a call names, wherever it named it. The schema says `path`, but the
   card also renders the calls that FAILED the schema -- a model that wrote
   `file_path` still told us the target, and the card titled with it (instead
   of with a bare +343 -0) is the difference between seeing what was attempted
   and guessing. */
function argPath(a) {
  for (const k of ['path', 'file_path', 'filename', 'file', 'target']) {
    if (a && typeof a[k] === 'string' && a[k]) return a[k];
  }
  return '';
}

/* One-line label for a call, derived from its arguments. The row carries a
   short label only; raw arguments live in the detail block. */
function actLabel(name, a, display) {
  if (display) return String(display);
  switch (name) {
    case 'read_file': case 'write_file': case 'edit_file':
      return wsShortPath(argPath(a));
    case 'list_dir': return a.path ? wsShortPath(a.path) + '/' : '';
    case 'grep': return String(a.pattern || '') + (a.glob ? '  ' + a.glob : '');
    case 'find': return String(a.pattern || '');
    case 'exec': return String(a.intent || a.command || '');
    case 'web_search': case 'deep_research': case 'tool_search':
      return a.query ? '“' + String(a.query) + '”' : '';
    case 'web_fetch': return String(a.url || '').replace(/^https?:\/\//, '');
    case 'understand_media': {
      const ps = Array.isArray(a.paths) ? a.paths.map((p) => String(p).split('/').pop()) : [];
      return ps.length > 2 ? `${ps[0]} ×${ps.length}` : ps.join(' ');
    }
    case 'spawn': return String(a.label || String(a.task || '').split('\n')[0]);
    case 'message': return a.channel ? '→ ' + String(a.channel) : String(a.content || '').split('\n')[0];
    case 'cron': return [a.action, a.cron_expr, a.every_seconds ? `${a.every_seconds}s` : '',
      String(a.message || '').split('\n')[0]].filter(Boolean).join(' · ');
    case 'use_skill': case 'read_skill': return String(a.skill_id || '');
    case 'image_generate': case 'video_generate': return String(a.prompt || '').split('\n')[0];
    case 'text_to_speech': return String(a.text || '').split('\n')[0];
    default: {
      const v = Object.values(a || {}).find((x) => typeof x === 'string' && x.trim());
      return v ? String(v) : '';
    }
  }
}

/* The line shown on a failed row: the first error-shaped line if there is
   one, else the first non-empty line. */
/* `cap` because the same summary serves a delegated run's status chip, where
   the column is narrow, and a failure card that owns the width of the page. */
const firstErrLine = (res, cap) => {
  const lines = String(res || '').split('\n').filter((x) => x.trim());
  const hit = lines.find((x) => /error|failed|traceback|could not|denied|exception/i.test(x));
  const l = hit || lines[0];
  return l ? shortArg(l.replace(/^\s*\[|\]\s*$/g, ''), cap || 62) : '';
};

function newStep() {
  const step = mk('div', 'step in');

  /* thinking: the thought streams in the open, the way the answer does, and
     folds to a quiet "thought · Ns" once work or prose takes over. Showing
     only its last line while it ran made the live thought read as one more
     tool row -- and the one thing a reader wants from a thought in flight is
     to be able to read it. */
  const row = mk('div', 'think tog');
  const lb = mk('span', 'lb', T('gui.think.label'));
  const ttm = mk('span', 'tm');
  row.append(lb, ttm, chev());
  row.hidden = true;
  const cot = mk('div', 'cot'); cot.hidden = true;
  let on = false;
  /* Once the reader works the fold themselves, the stream stops driving it. */
  let pinned = false;
  const set = (v) => { on = v; row.classList.toggle('open', on); cot.hidden = !on; };
  const flip = () => pinRow(row, () => { pinned = true; set(!on); });
  row.onclick = flip;
  row.tabIndex = 0;
  row.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); flip(); } };

  const say = mk('div', 'say prose');
  const wk = mk('div', 'wk');
  const wksum = mk('div', 'wrow tog'); wksum.hidden = true;
  const wkin = mk('div', 'wkin'); wkin.hidden = true;
  wk.append(wksum, wkin);
  step.append(row, cot, say, wk);
  stageBox().appendChild(step);

  /* The rows stay collapsed behind the summary from the first call onward, and
     the reader's own click outranks the stream from then on -- same rule as the
     thought above. */
  let wkPinned = false;
  const setWk = wireSummary(wksum, wkin, false);
  wksum.onclick = () => { wkPinned = true; setWk(wkin.hidden); };

  const st = {
    step, lb, cot, say, wk, wksum, wkin, set,
    calls: [], hasThink: false, hasSay: false, failed: false, thinkT0: 0, thinkMs: 0,
    reveal() {
      row.hidden = false;
      if (!row.classList.contains('live')) {
        row.classList.add('live');
        st.thinkT0 = Date.now();
        if (!pinned) set(true);
      }
      lb.textContent = T('gui.think.live');
      /* Follow the newest line, but only while the reader is already at the
         bottom of the thought -- scrolling back to read costs nothing then. */
      if (on && cot.scrollHeight - cot.scrollTop - cot.clientHeight < 40) {
        cot.scrollTop = cot.scrollHeight;
      }
      down();
    },
    /* Called by anything that supersedes the thought: a call, prose, the end
       of the step. Idempotent -- the clock must stop when thinking stopped,
       not keep counting the work that followed. */
    thinkDone(secs) {
      if (row.hidden) return;
      if (row.classList.contains('live')) {
        row.classList.remove('live');
        st.thinkMs += Date.now() - st.thinkT0;
        if (!pinned) set(false);
      }
      lb.textContent = T('gui.think.label');
      const s = secs != null ? secs : Math.round(st.thinkMs / 1000);
      if (s > 0) ttm.textContent = `${s}s`;
    },
    /* Repaint the stretch's one line. Called on every start and every landing,
       so the phrase grows with the run ("ran a command" -> "searched x4 - ran a
       command") instead of the rows stacking up. */
    paintWork() {
      if (!st.calls.length) return;
      /* One call is its own summary: a line above it would repeat the same verb
         and the same clock, and cost a click to reach the row it describes. */
      if (st.calls.length === 1) { wksum.hidden = true; wkin.hidden = false; return; }
      /* The step that just outgrew a single call folds its rows away for the
         first time -- from here on the summary line stands for the run. */
      if (wksum.hidden && !wkPinned) setWk(false);
      paintSummary(wksum, st.calls);
      /* A failure is the one thing worth opening unasked. */
      if (st.failed && !wkPinned && wkin.hidden) setWk(true);
    },
    tool(name, args, display) { st.thinkDone(); return newCall(st, name, args, display); },
    seal() { st.thinkDone(); foldWork(st); }
  };
  wksum.onkeydown = (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); wkPinned = true; setWk(wkin.hidden); }
  };
  return st;
}

/* ── delegation cards in the trail ────────────────────────────────
   Shared plumbing for the two signature calls. The dag card is bound to its
   run through these hooks: run_id is not in the call's arguments, it arrives
   on the first dag.* event, so the newest unbound card claims it. live.js
   feeds the events; the demo replay never calls this and the cards settle off
   the tool result alone. */
let delegOpenNode = null;    /* (run_id, node_id) => open the node's transcript; live.js wires it */
/* (run_id) => [{node, status}], read off disk. A card restored from history saw
   none of the dag.* events its chips are painted from, so without this its
   nodes sit pending forever on a run that finished days ago. */
let delegReadDag = null;
/* The run id is in the result line the tool already returns, so a restored card
   can recover the identity its live events would have given it and its chips
   become doors again. Two shapes arrive here and only one is persisted: the
   live preview is the tool's display text ("DAG <id>: ..."), while a reloaded
   turn carries its model text ("DAG run <id> finished: ..."). Reading only the
   first meant the restore path -- the only path this exists for -- never
   matched. */
const dagRunIdFrom = (res) => (/^DAG\s+(?:run\s+)?(\S+?)[:\s]/.exec(String(res || '')) || [])[1] || null;
let dagFlowPending = null;   /* the live dag card waiting to learn its run_id */
const dagFlowLive = new Map();

function dagFlowFeed(type, p) {
  if (!p) return;
  if (type === 'dag.run_started' && dagFlowPending) {
    dagFlowLive.set(p.run_id, dagFlowPending);
    dagFlowPending = null;
  }
  const f = dagFlowLive.get(p.run_id);
  if (!f) return;
  if (type === 'dag.run_started') f.start(p);
  else if (type === 'dag.node_updated') f.node(p);
  else if (type === 'dag.run_completed') { f.complete(p); dagFlowLive.delete(p.run_id); }
}

const DOT_OF = {
  pending: '', running: 'run', completed: 'ok',
  failed: 'bad', skipped: 'skip', interrupted: 'bad',
};

/* Where "open in workspace" lands for a spawn: live.js aims it at the run's
   own record in the panel; the demo replay just opens the panel. */
let delegOpenSpawn = null;

/* The delegation detail body: a labelled grid (task, target, state, cost) and
   an optional chip strip for a graph's nodes. The card used to end in a "view
   in workspace" button; the things on the card are the doors now -- a spawn's
   task line opens that run's transcript, a graph's chips open their nodes --
   so a separate button was a second, longer way to the same places. */
function delegDtl(rows, opts) {
  const d = mk('div', 'dtl dlg');
  const bd = mk('div', 'bd');
  const gr = mk('div', 'dgr');
  const els = {};
  rows.forEach(([key, label]) => {
    gr.appendChild(mk('div', 'k', label));
    els[key] = mk('div', 'v');
    gr.appendChild(els[key]);
  });
  bd.appendChild(gr);
  if (opts && opts.nds) bd.appendChild(opts.nds);
  if (opts && opts.open) {
    els.task.classList.add('gov');
    els.task.setAttribute('role', 'button');
    els.task.tabIndex = 0;
    els.task.title = T('gui.deleg.open_hint');
    els.task.onclick = (e) => { e.stopPropagation(); opts.open(); };
    els.task.onkeydown = (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); opts.open(); }
    };
  }
  d.appendChild(bd);
  return { d, els };
}

/* The state row's one element: a status dot and the word for it, with the
   first error line when the run failed. */
function delegState(el, state, err) {
  el.innerHTML = '';
  el.classList.toggle('err', state === 'bad');
  el.appendChild(mk('span', 'dot ' + state));
  const word = T(state === 'run' ? 'gui.deleg.st_run' : state === 'ok' ? 'gui.deleg.st_ok' : 'gui.deleg.st_bad');
  el.appendChild(document.createTextNode(state === 'bad' && err ? `${word} · ${err}` : word));
}

/* The delegation row itself: the same shape as every other call -- icon,
   verb, target, clock -- and the fold is live from the start, because a
   graph's chips are worth watching while it runs, not only after. */
function delegRow(st, c, dtl) {
  const row = mk('div', 'wrow run tog');
  const g = ico(actIco(c.name), 'ic');
  const vb = mk('span', 'vb');
  if (c.srv) vb.appendChild(mk('span', 'srv', `[${c.srv}] `));
  vb.appendChild(document.createTextNode(verbIng(c.name)));
  const ar = mk('span', 'ar', c.rowLabel || '');
  row.append(g, vb, ar, chev());
  st.wkin.appendChild(row);
  dtl.hidden = true;
  row.after(dtl);
  row.tabIndex = 0;
  const flip = () => pinRow(row, () => {
    dtl.hidden = !dtl.hidden;
    row.classList.toggle('open', !dtl.hidden);
  });
  row.onclick = flip;
  row.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); flip(); } };
  c.el = { row, g, vb, cv: null };
  return { row, g, vb };
}

function newSpawnCall(st, id) {
  const a = id.args || {};
  const c = {
    name: 'spawn', args: a, via: id.via, srv: id.srv,
    label: actLabel('spawn', a),
    done: false, ok: true, ms: 0, res: '', truncated: false, hunk: null,
  };
  st.calls.push(c);

  const who = a.agent ? String(a.agent) + (a.instance ? ' @' + a.instance : '') : T('gui.deleg.self');
  c.rowLabel = c.label ? `${who} · ${c.label}` : who;
  const { d, els } = delegDtl([
    ['task', T('gui.deleg.d_task')],
    ['agent', T('gui.deleg.d_agent')],
    ['state', T('gui.deleg.d_state')],
    ['cost', T('gui.deleg.d_cost')],
  ], {
    open: () => {
      if (delegOpenSpawn) delegOpenSpawn(a.agent ? String(a.agent) : '', c.label || '');
      else setWs(true, 'agents');
    },
  });
  els.task.textContent = c.label || String(a.task || '').slice(0, 160);
  els.agent.appendChild(mk('span', 'who', who));
  delegState(els.state, 'run');
  els.cost.textContent = '…';
  const { row, g, vb } = delegRow(st, c, d);
  st.paintWork();
  down();

  const t0 = Date.now();
  const tick = setInterval(() => {
    els.cost.textContent = dur(Date.now() - t0);
    if (Date.now() - t0 > 3600e3) clearInterval(tick);
  }, 1000);

  return {
    done(ok, res, ms, _diff, truncated) {
      clearInterval(tick);
      Object.assign(c, { done: true, ok, res: String(res == null ? '' : res), ms: ms || 0, truncated: !!truncated });
      tl.push({ name: c.name, arg: c.label, ms: c.ms, ok });
      raw.push(`tool.complete  spawn  ok=${ok}  ${c.ms}ms`);
      if (!ok) st.failed = true;
      row.classList.remove('run');
      vb.textContent = '';
      if (c.srv) vb.appendChild(mk('span', 'srv', `[${c.srv}] `));
      vb.appendChild(document.createTextNode(verb('spawn')));
      if (!ok) {
        row.classList.add('bad');
        if (g.firstChild) g.firstChild.setAttribute('d', ACT_ICO.bad);
      }
      delegState(els.state, ok ? 'ok' : 'bad', ok ? null : firstErrLine(c.res));
      els.cost.textContent = c.ms ? dur(c.ms) : '';
      st.paintWork();
      down();
    },
  };
}

function newDagCall(st, id) {
  const a = id.args || {};
  const nodes = Array.isArray(a.nodes) ? a.nodes.filter((n) => n && n.id) : [];
  const agents = [...new Set(nodes.map((n) => n.subagent).filter(Boolean))];
  const c = {
    name: 'run_subagent_dag', args: a, via: id.via, srv: id.srv,
    /* A call that never parsed into a graph has no counts worth stating. */
    label: nodes.length
      ? T('gui.deleg.dag_meta', { n: String(nodes.length), m: String(agents.length || 1) }) : '',
    done: false, ok: true, ms: 0, res: '', truncated: false, hunk: null,
  };
  st.calls.push(c);
  c.rowLabel = c.label;

  const nds = mk('div', 'nds');
  const chips = new Map();
  let runId = null;
  nodes.forEach((n) => {
    const chip = mk('button', 'nd');
    const dot = mk('span', 'dot');
    /* The node's own name leads and the agent runs under it, the way the graph
       draws a node -- the two are the same object seen twice, and a chip that
       said only the id made the reader open it to learn who was on it. */
    const txt = mk('span', 'tx');
    txt.appendChild(mk('span', 'id', n.id));
    if (n.subagent) txt.appendChild(mk('span', 'ag', n.subagent + (n.instance ? ' @' + n.instance : '')));
    chip.append(dot, txt);
    chip.title = n.subagent ? `${n.id} · ${n.subagent}` : n.id;
    /* Once the run has an id, every chip is a door to that node's transcript. */
    chip.onclick = (e) => { e.stopPropagation(); if (runId && delegOpenNode) delegOpenNode(runId, n.id); };
    chips.set(n.id, { chip, dot });
    nds.appendChild(chip);
  });
  const { d, els } = delegDtl([
    ['scale', T('gui.deleg.d_scale')],
    ['state', T('gui.deleg.d_state')],
    ['cost', T('gui.deleg.d_cost')],
  ], { nds: nodes.length ? nds : null });
  els.scale.textContent = c.label || T('gui.deleg.dag_title');
  delegState(els.state, 'run');
  els.cost.textContent = '…';
  const { row, g, vb } = delegRow(st, c, d);
  st.paintWork();
  down();

  const t0 = Date.now();
  const tick = setInterval(() => {
    els.cost.textContent = dur(Date.now() - t0);
    if (Date.now() - t0 > 3600e3) clearInterval(tick);
  }, 1000);

  const setDot = (nodeId, status) => {
    const at = chips.get(nodeId);
    if (!at) return;
    at.dot.className = 'dot' + (DOT_OF[status] ? ' ' + DOT_OF[status] : '');
    at.chip.dataset.st = status || 'pending';
    if (DOT_OF[status]) at.chip.classList.add('act');
  };
  /* A card the reader is seeing for the first time on a page reload: no dag.*
     event ever reached it, so its identity and every dot come off disk here.
     Guarded on already having an id, because a live card learned both from the
     events and asking again would be a round trip to be told what it knows. */
  const hydrate = () => {
    if (runId || !delegReadDag) return;
    const id = dagRunIdFrom(c.res);
    if (!id) return;
    runId = id;
    nds.dataset.live = '1';
    delegReadDag(id).then((rows) => {
      (rows || []).forEach((r) => setDot(r.node, r.status));
    }).catch(() => { /* a run whose dir is gone still lists its nodes */ });
  };
  const feeder = {
    start(p) { runId = p.run_id; nds.dataset.live = '1'; },
    node(p) { setDot(p.node, p.status); },
    complete(p) { (p.files || []).forEach((f) => setDot(f.node, f.status)); },
  };
  dagFlowPending = feeder;

  return {
    done(ok, res, ms, _diff, truncated) {
      clearInterval(tick);
      if (dagFlowPending === feeder) dagFlowPending = null;
      Object.assign(c, { done: true, ok, res: String(res == null ? '' : res), ms: ms || 0, truncated: !!truncated });
      hydrate();
      tl.push({ name: c.name, arg: c.label, ms: c.ms, ok });
      raw.push(`tool.complete  run_subagent_dag  ok=${ok}  ${c.ms}ms`);
      if (!ok) st.failed = true;
      row.classList.remove('run');
      vb.textContent = '';
      if (c.srv) vb.appendChild(mk('span', 'srv', `[${c.srv}] `));
      vb.appendChild(document.createTextNode(verb('run_subagent_dag')));
      if (!ok) {
        row.classList.add('bad');
        if (g.firstChild) g.firstChild.setAttribute('d', ACT_ICO.bad);
      }
      const counts = { ok: 0, bad: 0, skip: 0 };
      chips.forEach(({ dot }) => {
        if (dot.classList.contains('ok')) counts.ok += 1;
        else if (dot.classList.contains('bad')) counts.bad += 1;
        else if (dot.classList.contains('skip')) counts.skip += 1;
      });
      const bits = [];
      if (counts.ok || counts.bad || counts.skip) {
        bits.push(T('gui.deleg.dag_done', { ok: String(counts.ok) }));
        if (counts.bad) bits.push(T('gui.deleg.dag_bad', { n: String(counts.bad) }));
        if (counts.skip) bits.push(T('gui.deleg.dag_skip', { n: String(counts.skip) }));
      }
      delegState(els.state, ok ? 'ok' : 'bad', ok ? null : firstErrLine(c.res));
      if (ok && bits.length) {
        els.state.appendChild(document.createTextNode(` · ${bits.join(' · ')}`));
      }
      els.cost.textContent = c.ms ? dur(c.ms) : '';
      st.paintWork();
      down();
    },
  };
}

/* One activity row per call: spinner + present-tense verb + ticking clock
   while it runs; a settled verb, chips and duration once it lands. */
function newCall(st, rawName, rawArgs, display) {
  const id = actId(rawName, rawArgs);
  if (id.name === 'spawn') return newSpawnCall(st, id);
  if (id.name === 'run_subagent_dag') return newDagCall(st, id);
  const c = {
    name: id.name, args: id.args, via: id.via, srv: id.srv,
    label: actLabel(id.name, id.args, display),
    done: false, ok: true, ms: 0, res: '', truncated: false, hunk: null,
  };
  /* Restored history carries no arguments — a hunk computed from nothing
     would show a lying "+1 −0" chip over one blank line. */
  if (id.name === 'edit_file' && typeof id.args.old_text === 'string' && typeof id.args.new_text === 'string') {
    c.hunk = hunkFromEdit(id.args.old_text, id.args.new_text);
  }
  if (id.name === 'write_file' && typeof id.args.content === 'string') {
    c.hunk = hunkFromWrite(id.args.content);
  }
  st.calls.push(c);

  const row = mk('div', 'wrow run' + (id.name === 'ask_user' ? ' wait' : ''));
  const g = ico(actIco(id.name), 'ic');
  const vb = mk('span', 'vb');
  if (c.srv) vb.appendChild(mk('span', 'srv', `[${c.srv}] `));
  vb.appendChild(document.createTextNode(verbIng(c.name)));
  /* Hidden until the call settles and turns out to have a detail block; it
     then becomes the fold caret, the last thing on the line like every other
     caret in the transcript. */
  const cv = mk('span', 'cvslot');
  row.append(g, vb, cv);
  st.wkin.appendChild(row);
  st.paintWork();
  down();

  c.el = { row, g, vb, cv };
  return {
    done(ok, res, ms, diff, truncated) {
      Object.assign(c, {
        done: true, ok, res: String(res == null ? '' : res),
        ms: ms || 0, truncated: !!truncated,
      });
      if (diff) c.hunk = hunkFromUnified(diff);
      tl.push({ name: c.name, arg: c.label, ms: c.ms, ok });
      raw.push(`tool.complete  ${c.name}  ok=${ok}  ${c.ms}ms`);
      if (!ok) st.failed = true;
      settleCall(st, c);
      st.paintWork();
      down();
    }
  };
}

/* A call row carries no clock, running or settled. How long a tool took is not
   what the reader is scanning this column for, and a number trailing every verb
   turned a stretch of work into a table of timings. The turn's own span is
   still on the fold that closes it. */
function settleCall(st, c) {
  const { row, g, vb, cv } = c.el;
  row.classList.remove('run', 'wait');
  vb.textContent = '';
  if (c.srv) vb.appendChild(mk('span', 'srv', `[${c.srv}] `));
  vb.appendChild(document.createTextNode(verb(c.name)));
  if (c.hunk && (c.hunk.add || c.hunk.del)) {
    const dn = mk('span', 'diffn');
    dn.append(mk('span', 'a', `+${c.hunk.add}`), document.createTextNode(' '),
      mk('span', 'd', `-${c.hunk.del}`));
    row.insertBefore(dn, cv);
  }
  if (!c.ok) {
    row.classList.add('bad');
    if (g.firstChild) g.firstChild.setAttribute('d', ACT_ICO.bad);
    const first = firstErrLine(c.res);
    if (first) row.insertBefore(mk('span', 'err', first), cv);
  }
  const dtl = dtlFor(c);
  if (!dtl) { cv.remove(); return; }
  dtl.hidden = true;
  row.after(dtl);
  row.classList.add('tog');
  cv.replaceWith(chev());
  row.tabIndex = 0;
  const flip = () => pinRow(row, () => {
    dtl.hidden = !dtl.hidden;
    row.classList.toggle('open', !dtl.hidden);
  });
  row.onclick = flip;
  row.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); flip(); } };
}

/* A <pre> whose URLs and file paths are doors, not characters. A tool's output
   is full of places -- the URL it fetched, the file it wrote -- and the reader's
   next act is usually to go to one of them; copying the string out to do it by
   hand is the friction this removes. URLs open a tab; an absolute path opens in
   the page's own file viewer. Paths are matched conservatively (rooted, no
   spaces, an extension) because a false link on prose is worse than a missed
   link on an odd filename. */
const PRE_URL = /https?:\/\/[^\s<>"')\]]+[^\s<>"')\].,;:!?]/g;
const PRE_PATH = /(?:^|[\s('"[])((?:~|\/)[\w.\-/@]+\.[A-Za-z0-9]{1,8})(?=$|[\s)'"\],;:])/g;
function preLinked(text) {
  const pre = mk('pre');
  const s = String(text);
  const marks = [];
  let m;
  while ((m = PRE_URL.exec(s)) !== null) marks.push({ at: m.index, len: m[0].length, url: m[0] });
  while ((m = PRE_PATH.exec(s)) !== null) {
    const at = m.index + m[0].length - m[1].length;
    marks.push({ at, len: m[1].length, path: m[1] });
  }
  if (!marks.length) { pre.textContent = s; return pre; }
  marks.sort((x, y) => x.at - y.at);
  let done = 0;
  marks.forEach((k) => {
    if (k.at < done) return;
    pre.appendChild(document.createTextNode(s.slice(done, k.at)));
    const piece = s.substr(k.at, k.len);
    if (k.url) {
      const a = document.createElement('a');
      a.href = k.url; a.target = '_blank'; a.rel = 'noopener'; a.textContent = piece;
      a.onclick = (e) => e.stopPropagation();
      pre.appendChild(a);
    } else {
      const b = mk('button', 'pth', piece);
      /* `.pth` is a contract, not a style hook: the document-level Enter/Space
         handler opens `dataset.p`. Without it, keyboard activation asks the
         viewer for a file named "undefined". */
      b.dataset.p = k.path;
      b.onclick = (e) => { e.stopPropagation(); pathOpen(k.path); };
      pre.appendChild(b);
    }
    done = k.at + k.len;
  });
  pre.appendChild(document.createTextNode(s.slice(done)));
  return pre;
}

/* The detail block. Per-tool: a diff for edits, `$ command` + tail + exit
   chip for exec, a clickable link for fetches, plain preview otherwise. */
function dtlFor(c) {
  const d = mk('div', 'dtl');
  const bd = mk('div', 'bd');
  const addPre = (text) => {
    let lines = String(text || '').split('\n');
    while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
    if (!lines.length) return;
    const over = lines.length - DTL_MAX_LINES;
    if (over > 0) lines = lines.slice(0, DTL_MAX_LINES);
    bd.appendChild(preLinked(lines.join('\n')));
    if (over > 0) bd.appendChild(mk('div', 'more', `… +${over}`));
  };
  /* Every card is titled: what it is, what it changed, and one click to take
     the raw text with you. */
  const head = (name, hunk, copyText, openPath) => {
    const h = mk('div', 'dhd');
    /* A card titled with a path is the shortest route from "the agent wrote
       this" to reading it, so the title opens the viewer rather than being a
       label you then go hunting for in the panel. */
    if (openPath) {
      const b = mk('button', 'nm pth', name);
      b.dataset.p = openPath;
      b.title = openPath;
      b.onclick = (e) => { e.stopPropagation(); pathOpen(openPath); };
      h.appendChild(b);
    } else {
      h.appendChild(mk('span', 'nm', name));
    }
    if (hunk && (hunk.add || hunk.del)) {
      const ct = mk('span', 'ct');
      ct.append(mk('span', 'a', `+${hunk.add}`), document.createTextNode(' '),
        mk('span', 'd', `-${hunk.del}`));
      h.appendChild(ct);
    }
    if (copyText) {
      const b = tipBtn('<rect x="9" y="9" width="11" height="11" rx="2.4"/>'
        + '<path d="M15 5.5A1.5 1.5 0 0 0 13.5 4H6a2 2 0 0 0-2 2v7.5A1.5 1.5 0 0 0 5.5 15"/>',
      T('gui.dtl.copy'), 'cp');
      b.onclick = (e) => {
        e.stopPropagation();
        if (navigator.clipboard) navigator.clipboard.writeText(copyText);
        tipFlash(b, T('gui.answer.copied'));
      };
      h.appendChild(b);
    }
    d.appendChild(h);
  };

  if (c.name === 'edit_file' || c.name === 'write_file') {
    if (c.hunk && c.hunk.rows.length) {
      const rows = c.hunk.rows.slice(0, 120);
      /* The target leads, always -- +N -M alone as a first line says what
         happened to nothing. A call that failed validation without naming any
         file still gets a worded title over a bare count. */
      const fp = argPath(c.args);
      head(wsShortPath(fp) || T('gui.dtl.plain'), c.hunk,
        rows.map((r) => (r[0] === 'gap' ? '' : r[1])).join('\n'), fp);
      rows.forEach((r) => {
        if (r[0] === 'gap') bd.appendChild(mk('div', 'dline gap', `⋯ ${r[1].length}`));
        else if (r[0] === 'hunk') bd.appendChild(mk('div', 'dline gap', '⋯'));
        else bd.appendChild(mk('div', 'dline' + (r[0] === 'add' ? ' a' : r[0] === 'del' ? ' d' : ''), r[1] || ' '));
      });
    }
    if (!c.ok) addPre(c.res);
  } else if (c.name === 'exec') {
    const cmd = String(c.args.command || '');
    head(cmd || T('gui.dtl.output'), null, cmd || c.res);
    if (cmd) bd.appendChild(mk('div', 'cmd', cmd));
    /* the shell appends its own "Exit code: N" line — lift it into a chip */
    const lines = String(c.res || '').trimEnd().split('\n');
    const m = /^Exit code:\s*(-?\d+)$/.exec(lines[lines.length - 1] || '');
    let exit = null;
    if (m) { exit = Number(m[1]); lines.pop(); }
    addPre(lines.join('\n'));
    if (exit != null) {
      const w = mk('div');
      w.style.marginTop = '6px';
      w.appendChild(mk('span', 'exit ' + (exit === 0 ? 'ok' : 'bad'), T('gui.dtl.exit', { n: exit })));
      bd.appendChild(w);
    }
  } else if (c.name === 'web_fetch') {
    const url = String(c.args.url || '');
    head(url || T('gui.dtl.plain'), null, url || c.res);
    if (url) {
      const aEl = document.createElement('a');
      aEl.href = url; aEl.target = '_blank'; aEl.rel = 'noopener'; aEl.textContent = url;
      bd.appendChild(aEl);
    }
    addPre(c.res);
  } else {
    /* The target titles the card -- it is no longer on the row, and "output" as
       a title tells the reader nothing about which file or pattern this was. */
    let title = shortArg(c.label, 120);
    if (!title && (c.via || c.srv)) title = shortArg(JSON.stringify(c.args), 120);
    if (c.via) title = T('gui.dtl.via') + (title ? ' · ' + title : '');
    /* A read's title is its file, and the title already knows how to open one
       -- list_dir excepted, because the viewer renders files, and handing it a
       directory shows an error for a click that looked legitimate. */
    const fp = typeof c.args.path === 'string' && c.name !== 'list_dir' ? c.args.path : '';
    head(title || T('gui.dtl.plain'), null, String(c.res || ''), fp);
    addPre(c.res);
  }
  /* An ellipsis, not a sentence. The old note said the output was "truncated
     upstream", which named a culprit that does not exist: nothing upstream cut
     anything -- the model received the whole result, and this card was only
     ever sent its head. A mark that the text continues is the true statement,
     and it is also the quieter one. */
  if (c.truncated) bd.appendChild(mk('div', 'trunc', '…'));
  /* The row carries the verb alone, so the argument has nowhere else to live:
     a card that is only a title is still the one place to read what ran. */
  if (!d.querySelector('.dhd') && c.label) {
    const fp = typeof c.args.path === 'string' ? c.args.path : '';
    head(shortArg(c.label, 160), null, c.label, fp);
  }
  if (bd.childElementCount) d.appendChild(bd);
  return d.childElementCount ? d : null;
}

/* One line for a stretch of calls: what ran, how many of each, how long. It is
   painted while the calls are still landing and again once they settle, so a
   running stretch reads as one growing line instead of a column of rows with
   their arguments spelled out -- and the run never changes shape as it ends.
   A stretch with a failure opens itself, and folding it by hand keeps the
   failure chip on the summary: the fold never hides what went wrong. */
function paintSummary(sum, calls) {
  const live = calls.some((c) => !c.done);
  sum.innerHTML = '';
  sum.classList.add('sum');
  sum.classList.toggle('run', live);
  const one = calls.every((c) => c.name === calls[0].name);
  sum.appendChild(ico(one ? actIco(calls[0].name) : ACT_ICO.dot, 'ic'));
  sum.appendChild(mk('span', 'ar', phraseOf(calls)));
  const f = calls.filter((c) => c.done && !c.ok).length;
  if (f) sum.appendChild(mk('span', 'chip bad', T('gui.n_failed', { n: f })));
  sum.appendChild(chev());
  sum.hidden = false;
}

function wireSummary(sum, body, startOpen) {
  let open;
  const setOpen = (v) => { open = v; sum.classList.toggle('open', open); body.hidden = !open; };
  /* Pinned on the reader's own toggles only. The stream calls setOpen too (a
     failure opens its run), and pinning there would fight the tail-follow. */
  sum.onclick = () => pinRow(sum, () => setOpen(!open));
  sum.tabIndex = 0;
  sum.onkeydown = (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    pinRow(sum, () => setOpen(!open));
  };
  setOpen(!!startOpen);
  return setOpen;
}

function buildSummary(sum, calls, body, startOpen) {
  paintSummary(sum, calls);
  wireSummary(sum, body, startOpen);
}

/* The stretch was already one line while it ran, so sealing only repaints it as
   settled. It deliberately does NOT expand short runs: a two-call stretch that
   unfolded itself the moment the turn ended would change shape under the
   reader's eyes, for two rows they can open with one click. */
function foldWork(st) {
  st.paintWork();
}

/* Verbs and counts only -- no targets. The folded line answers "what kind of
   work happened here"; a 26-character slice of a regex or a shell pipeline
   answers nothing, and the full argument is on the row one click below.
   Counted over the whole stretch rather than per adjacent run: interleaved
   calls read as "ran a command - read - ran a command", which says the same
   verb twice and makes the reader count. Order is first appearance. */
function phraseOf(calls) {
  const n = new Map();
  calls.forEach((c) => n.set(c.name, (n.get(c.name) || 0) + 1));
  return [...n].map(([name, k]) => (k === 1
    ? verb(name)
    : T('gui.act.n.' + name, { n: k }, `${verb(name)} ×${k}`))).join(' · ');
}

/* Once the answer has landed, everything that led to it — thinking, mid-turn
   narration, activity rows — collapses behind one line. The answer is what the
   reader came back for; the work stays one click away. Scanning back from the
   tail stops at the turn's own boundary, so earlier turns keep their folds.

   A turn gets ONE fold: work that lands after an early fold joins that fold
   rather than stacking a second header, so a turn that alternated between
   narrating and working does not read as a column of "done" lines. */
/* "done" and how long it took are two readings, so they are two spans: the
   clock is the same faint mono as every other elapsed time on the page, and an
   empty one collapses instead of leaving a gap before the caret. */
function foldTime(fold, time) {
  const lb = fold.querySelector('.tfh > .lb');
  const tm = fold.querySelector('.tfh > .tm');
  if (lb) lb.textContent = T('gui.fold.done');
  if (tm) tm.textContent = time || '';
}

function collapseTurn(time) {
  const stage = stageBox();
  const nodes = [];
  let open_ = null;
  for (let n = stage.lastElementChild; n; n = n.previousElementSibling) {
    if (n.classList.contains('ask')) break;
    if (n.classList.contains('tfold')) { open_ = n; break; }
    if (n.classList.contains('step')) nodes.unshift(n);
  }
  if (!nodes.length) return null;
  if (open_) {
    const body = open_.querySelector('.tfb');
    nodes.forEach((n) => body.appendChild(n));
    foldTime(open_, time);
    return open_;
  }
  /* Every turn that led somewhere folds the same way, thinking included: one
     "done" line is what the reader looks for after the answer, and a turn that
     only thought must not be the one exception that stays expanded.
     An empty step is still not worth a header -- a caret over nothing. */
  const solid = (n) => !!n.querySelector('.wrow:not([hidden]), .think:not([hidden])')
    || !!(n.querySelector('.say') && n.querySelector('.say').textContent.trim());
  if (!nodes.some(solid)) return null;

  const fold = mk('div', 'tfold');
  const head = mk('button', 'tfh');
  head.append(mk('span', 'lb'), mk('span', 'tm'), chev());
  head.setAttribute('aria-label', T('gui.fold.aria'));
  const body = mk('div', 'tfb');
  nodes[0].before(fold);
  nodes.forEach((n) => body.appendChild(n));
  fold.append(head, body);
  foldTime(fold, time);

  let open = false;
  const set = (v) => {
    open = v;
    body.hidden = !open;
    fold.classList.toggle('open', open);
    head.setAttribute('aria-expanded', String(open));
  };
  head.onclick = () => pinRow(head, () => set(!open));
  set(false);
  return fold;
}

/* Consecutive silent steps read as one stretch of work: their rows move under
   a single summary line — the work segment spans episode boundaries. */
function foldSilentRuns(steps) {
  let i = 0;
  while (i < steps.length) {
    if (!isSilent(steps[i])) { i += 1; continue; }
    let j = i;
    while (j < steps.length && isSilent(steps[j])) j += 1;
    if (j - i >= MIN_RUN_STEPS) mergeRun(steps.slice(i, j));
    i = j;
  }
}
const isSilent = (st) => !st.hasSay && !st.hasThink && !st.hasQA && !st.failed && st.calls.length > 0;

function mergeRun(group) {
  const calls = group.flatMap((st) => st.calls);
  if (!calls.length || !calls.every((c) => c.done)) return;
  const holder = mk('div', 'step in');
  const wk = mk('div', 'wk');
  const sum = mk('div', 'wrow tog');
  const body = mk('div', 'wkin');
  wk.append(sum, body);
  holder.appendChild(wk);
  group.forEach((st) => { [...st.wkin.children].forEach((n) => body.appendChild(n)); });
  buildSummary(sum, calls, body, false);
  group[0].step.replaceWith(holder);
  group.slice(1).forEach((st) => st.step.remove());
}

/* An ask_user round trip is part of the work, not a message the reader typed:
   question and answer share one row, beside the activity rows of the tool that
   asked. Echoing only the answer, as a user bubble, lost the question entirely
   and put an `.ask` node in the middle of the turn -- which reads as a new turn,
   and since folding stops at a turn boundary it stranded every row before it
   outside the fold. Long text ellipsizes and the full pair is one click away. */
const QA_Q = 62, QA_A = 34;
function askEcho(question, answer, opts) {
  const o = opts || {};
  const q = String(question || '').replace(/\s+/g, ' ').trim();
  const a = String(answer || '').replace(/\s+/g, ' ').trim();
  const row = mk('div', 'wrow qa');
  row.append(ico(ACT_ICO.ask, 'ic'),
    mk('span', 'vb', T(o.skipped ? 'gui.qa.skipped' : 'gui.qa.answered')),
    mk('span', 'ar', shortArg(q, QA_Q)));
  if (a && !o.skipped) row.appendChild(mk('span', 'an', shortArg(a, QA_A)));
  const cv = mk('span', 'cvslot');
  row.appendChild(cv);
  (o.host || stageBox()).appendChild(row);
  if (q.length <= QA_Q && a.length <= QA_A) { cv.remove(); down(); return row; }

  const dtl = mk('div', 'dtl');
  const bd = mk('div', 'bd');
  bd.append(mk('div', 'hd', T('gui.qa.q')), mk('pre', null, q));
  if (a) bd.append(mk('div', 'hd', T('gui.qa.a')), mk('pre', null, a));
  dtl.appendChild(bd);
  dtl.hidden = true;
  row.after(dtl);
  row.classList.add('tog');
  cv.replaceWith(chev());
  row.tabIndex = 0;
  const flip = () => pinRow(row, () => { dtl.hidden = !dtl.hidden; row.classList.toggle('open', !dtl.hidden); });
  row.onclick = flip;
  row.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); flip(); } };
  down();
  return row;
}

/* The turn is never silent: a breathing dot fills the dead air between
   pressing send and the first event. Any content kills it. */
let statusEl = null;
function showStatus(text) {
  killStatus();
  statusEl = mk('div', 'status in');
  statusEl.append(mk('span', 'pip'), mk('span', null, text));
  $('#stage').appendChild(statusEl);
  down();
}
function killStatus() { if (statusEl) { statusEl.remove(); statusEl = null; } }

/* When an answer landed. Today's answers need only a clock; anything older
   needs the date to mean anything, so it carries the full stamp. */
function stamp(when) {
  const d = when instanceof Date ? when : new Date(when);
  if (!d || isNaN(d.getTime())) return '';
  const p = (n) => String(n).padStart(2, '0');
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
  const now = new Date();
  const sameYear = d.getFullYear() === now.getFullYear();
  const today = sameYear && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  if (today) return hm;
  /* The year only earns its place once it is not the current one: four digits on
     every line of an afternoon's work is four digits of nothing. */
  const md = `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}`;
  return sameYear ? md : `${d.getFullYear()}-${md}`;
}

function answerBlock(text) {
  const box = mk('div', 'answer in');
  const body = mk('div', 'prose');
  const acts = mk('div', 'acts');
  /* Icons, like the live footer: the row is 27px squares, and a word in one of
     them wraps to a column of single characters. */
  const add = (label, path, fn) => {
    const b = mk('button');
    b.dataset.tip = label;
    b.setAttribute('aria-label', label);
    b.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
    b.onclick = fn;
    acts.appendChild(b);
  };
  add(T('gui.answer.copy'), COPY_ICO,
    () => { navigator.clipboard && navigator.clipboard.writeText(text); toast('已复制到剪贴板'); });
  /* Not inside a sub-agent transcript: branching acts on the OPEN session, and
     a delegated run has no session of its own to fork. Same guard as live. */
  if (!stageHost) {
    add(T('gui.answer.branch'), '<circle cx="7" cy="6" r="2.2"/><circle cx="7" cy="18" r="2.2"/>'
      + '<circle cx="17" cy="8" r="2.2"/><path d="M7 8.2v7.6"/>'
      + '<path d="M17 10.2v1.3a3.5 3.5 0 0 1-3.5 3.5H10"/>', () => {
      const s = { id: 'n' + Date.now(), title: (sess(cur) ? sess(cur).title : '新任务') + ' 的分支',
        last: '从上一轮回复分叉', when: '刚刚', run: null };
      SESS.unshift(s); cur = s.id; drawList(); openSession(s); toast('已分叉出新会话');
    });
  }
  box.append(body, acts);
  stageBox().appendChild(box);
  return body;
}

