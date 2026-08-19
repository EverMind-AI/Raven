/* -- external agents: the rpc source ---------------------------------
   `subagents.*` is one surface shared with the TUI and the web UI: the rows,
   the install grouping and the write path all live server-side, so this layer
   only maps a row into what the page draws and sends the mutation back. The
   page (demo/120-capabilities.js) owns the renderers and every flag they read;
   installing onto the seam replaces the fixture source before the first paint.

   The list is re-fetched after every mutation rather than patched locally: the
   handler recomputes `group`, `enabled` and the probe verdict together, and a
   client that guesses any one of them is how the page starts disagreeing with
   the config on disk. `probe: false` on that follow-up call skips the
   availability check, which can cost up to ten seconds per entry and would only
   re-measure what the write just changed. */
function xaRowOf(r) {
  return {
    name: r.name,
    preset: r.preset,
    kind: r.kind || 'cli',
    configured: !!r.configured,
    enabled: !!r.enabled,
    probe_status: r.probe_status || 'unknown',
    upgrade_to: r.upgrade_to || null,
    probe_detail: r.probe_detail || '',
    has_api_key: !!r.has_api_key,
    description: r.description || '',
    last_test_ok: r.last_test_ok,
    last_test_detail: r.last_test_detail || '',
    last_test_at_ms: r.last_test_at_ms || null,
    test_running: !!r.test_running,
  };
}

/* What the last fetch reported, kept here rather than read back off the page:
   the carry-over below is a fact about this transport (a probe-less list says
   "unknown" for every row), so the source answers it from its own memory
   instead of reaching into the array the page is rendering. */
let xaSeen = new Map();

async function xaFetch(probe) {
  const res = await rpc.call('subagents.list', { probe: !!probe });
  /* A probe-less list reports every row as "unknown", which would blank the
     health line of a row that was ready a second ago -- connecting an agent
     would look like it broke it. The verdict cannot have changed by writing
     config, so the last known one is carried over. */
  const rows = (res.rows || []).map((r) => {
    const row = xaRowOf(r);
    const prev = xaSeen.get(row.name);
    if (row.probe_status === 'unknown' && prev && prev.probe_status !== 'unknown') {
      row.probe_status = prev.probe_status;
      row.probe_detail = prev.probe_detail;
    }
    return row;
  });
  xaSeen = new Map(rows.map((r) => [r.name, r]));
  return rows;
}

DS.xa = {
  load: (probe) => xaFetch(!!probe),
  act: async (op, row, args) => {
    const a = args || {};
    if (op === 'connect') {
      /* Only name / description / key travel: every execution field comes from
         the preset server-side. A page that could post a command line would make
         "which agent is this" unanswerable. */
      await rpc.call('subagents.add', {
        preset: row.preset || row.name,
        name: a.new_name || undefined,
        description: a.description || undefined,
        api_key: a.api_key || undefined,
      });
    } else if (op === 'update') {
      await rpc.call('subagents.update', {
        name: row.name,
        new_name: a.new_name && a.new_name !== row.name ? a.new_name : undefined,
        description: a.description,
        api_key: a.api_key || undefined,
      });
    } else if (op === 'toggle') {
      await rpc.call('subagents.toggle', { name: row.name, enabled: !!a.enabled });
    } else if (op === 'remove') {
      await rpc.call('subagents.remove', { name: row.name });
    } else if (op === 'upgrade') {
      /* There is no "change the transport" write: `subagents.update` touches name,
         description and key only, on purpose. So the switch is a remove plus an add
         from the preset, which is also what makes it visible in config as one
         entry replaced rather than an entry mutated underneath its session handles. */
      await rpc.call('subagents.remove', { name: row.name });
      await rpc.call('subagents.add', {
        preset: row.preset || row.name,
        name: row.name,
        description: row.description || undefined,
      });
    } else if (op === 'test_cancel') {
      await rpc.call('subagents.test_cancel', { name: row.name });
    } else if (op === 'test') {
      /* The call runs the agent for real and does not return until it answers.
         The page marks the row running and redraws before handing over, so the
         button does not look dead for the length of a model turn. */
      const res = await rpc.call('subagents.test', { name: row.name, source: row.configured ? 'config' : 'preset' });
      if (!res.ok && res.detail && !res.cancelled) toast(`${row.name}: ${res.detail}`);
      /* probe:true here, unlike every other mutation: a test is the one write that
         changes the probe verdict. An acp test records the capability snapshot the
         probe reads, so carrying the old "not recorded yet" over would leave the
         row telling the user to run the test they just ran. */
      return xaFetch(true);
    }
    return xaFetch(false);
  },
};

/* ── the dag sheet: what a `run_subagent_dag` call is orchestrating ────────
   The transcript only ever shows this call as one tool row with a clamped
   result, and the three `dag.*` events that describe the graph arrived here and
   were dropped on the floor. They are the whole picture of the work, so they get
   the same place the clarify sheet gets: above the composer, on the turn being
   worked on rather than buried in the scrollback. */
const DAGS = new Map();  // session key -> the graph that conversation is running
const dagFor = (key) => DAGS.get(key || sheetSession()) || null;

/* Wider and taller than the first pass: the box now carries a status mark as
   well as the node's id, the agent under it and a clock, and 108x38 had them
   touching each other. GAP_X leaves 46px of edge between columns, which is
   enough for a curve to read as a curve rather than as a kink. */
const DAG_GAP_X = 182;
const DAG_GAP_Y = 60;
const DAG_W = 136;
const DAG_H = 44;
const DAG_PAD = 10;

/* Depth by longest path, which is what puts a node in the column after the last
   thing it waits for. Memoised, and guarded against a cycle it should never see:
   the server rejects a cyclic graph before running it, but a panel that hangs is
   a worse way to find that out than a panel that draws something odd. */
function dagDepths(nodes) {
  const by = new Map(nodes.map((n) => [n.id, n]));
  const depth = new Map();
  const walking = new Set();
  const of = (id) => {
    if (depth.has(id)) return depth.get(id);
    const n = by.get(id);
    const deps = (n && n.depends_on) || [];
    if (!n || !deps.length || walking.has(id)) { depth.set(id, 0); return 0; }
    walking.add(id);
    let d = 0;
    deps.forEach((p) => { if (by.has(p)) d = Math.max(d, of(p) + 1); });
    walking.delete(id);
    depth.set(id, d);
    return d;
  };
  nodes.forEach((n) => of(n.id));
  return depth;
}

/* One sentence for a reader who does not want to read a graph: how much work,
   how deep, who is doing it, and whether anything actually runs side by side. */
function dagGist(d) {
  const nodes = d.order.map((id) => d.nodes.get(id));
  const depth = dagDepths(nodes);
  const layers = new Map();
  nodes.forEach((n) => {
    const k = depth.get(n.id) || 0;
    layers.set(k, (layers.get(k) || 0) + 1);
  });
  const widest = Math.max(...layers.values(), 1);
  const agents = [...new Set(nodes.map((n) => n.subagent).filter(Boolean))];
  const bits = [T('gui.dag.count').replace('{n}', String(nodes.length)).replace('{d}', String(layers.size))];
  bits.push(widest > 1 ? T('gui.dag.parallel').replace('{n}', String(widest)) : T('gui.dag.serial'));
  if (agents.length) bits.push(agents.join(' · '));
  return bits.join(' · ');
}

const SVG_NS = 'http://www.w3.org/2000/svg';

function svgEl(tag, attrs, cls) {
  const e = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs || {}).forEach(([k, v]) => e.setAttribute(k, String(v)));
  if (cls) e.setAttribute('class', cls);
  return e;
}

/* The one mark a node wears, and the whole of how status is drawn. It used to be
   the box's own stroke colour plus a pulse of the entire node's opacity: with
   nothing but colour to go by, a reader had to already know the palette, and a
   node fading in and out as a whole read as an error rather than as work. Colour
   stays, quietly, on the border; this is what a reader actually looks at.

   Running is the same three bars the turn's own row and a sub-agent row wear
   (workGlyphSvg), which is the point -- one glyph for work in progress, whatever
   is doing the work. */
function dagMark(status, cx, cy) {
  if (status === 'running') return workGlyphSvg(cx - 5, cy + 4);
  if (status === 'completed') return svgEl('path', { d: `M${cx - 5} ${cy}l3.6 3.8 6.4 -7.6` }, 'mk ok');
  if (status === 'failed') return svgEl('path', { d: `M${cx - 4} ${cy - 4}l8 8M${cx + 4} ${cy - 4}l-8 8` }, 'mk bad');
  if (status === 'skipped' || status === 'interrupted') {
    return svgEl('path', { d: `M${cx - 4.5} ${cy}h9` }, 'mk skip');
  }
  return svgEl('circle', { cx, cy, r: 3.6 }, 'mk wait');
}

/* Column x, row y, and the sizes that follow from them. Each column is centred
   on the graph's own midline rather than stacked from the top: a fan-out into
   three and a fan-in back to one then reads as the diamond it is, instead of a
   staircase whose single nodes sit against the ceiling with their edges cutting
   diagonally down. */
function dagLayout(nodes) {
  const depth = dagDepths(nodes);
  const cols = new Map();
  nodes.forEach((n) => {
    const c = depth.get(n.id) || 0;
    if (!cols.has(c)) cols.set(c, []);
    cols.get(c).push(n);
  });
  const tallest = Math.max(...[...cols.values()].map((c) => c.length), 1);
  const height = DAG_PAD * 2 + tallest * DAG_H + (tallest - 1) * (DAG_GAP_Y - DAG_H);
  const width = DAG_PAD * 2 + (cols.size - 1) * DAG_GAP_X + DAG_W;
  const at = new Map();
  cols.forEach((column, c) => {
    const span = column.length * DAG_H + (column.length - 1) * (DAG_GAP_Y - DAG_H);
    const top = (height - span) / 2;
    column.forEach((n, i) => {
      at.set(n.id, { x: DAG_PAD + c * DAG_GAP_X, y: top + i * DAG_GAP_Y });
    });
  });
  return { at, width, height };
}

function dagSvg(d) {
  const nodes = d.order.map((id) => d.nodes.get(id)).filter(Boolean);
  const { at, width, height } = dagLayout(nodes);
  const svg = svgEl('svg', { width, height, viewBox: `0 0 ${width} ${height}` });

  /* Edges first so a box always sits on top of the line reaching it. An edge out
     of a node that has not finished is drawn faint: what has actually flowed
     through the graph so far is the thing a reader is trying to see. */
  nodes.forEach((n) => {
    (n.depends_on || []).forEach((pid) => {
      const a = at.get(pid);
      const b = at.get(n.id);
      if (!a || !b) return;
      const x1 = a.x + DAG_W;
      const y1 = a.y + DAG_H / 2;
      const x2 = b.x - 5;
      const y2 = b.y + DAG_H / 2;
      const mid = (x1 + x2) / 2;
      const done = (d.nodes.get(pid) || {}).status === 'completed';
      // Which node this edge leaves, so a later status change can darken it in
      // place instead of costing the graph a redraw.
      svg.appendChild(svgEl(
        'path',
        { d: `M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`, 'data-from': pid },
        'edge' + (done ? ' flowed' : ''),
      ));
      // The head, drawn separately: a marker-end would inherit the path's own
      // stroke width and end up heavier than the line it caps.
      svg.appendChild(svgEl(
        'path',
        { d: `M${x2 - 3.5} ${y2 - 3}L${x2 + 1} ${y2}l-4.5 3`, 'data-from': pid },
        'tip' + (done ? ' flowed' : ''),
      ));
    });
  });

  d.els = new Map();
  nodes.forEach((n) => {
    const p = at.get(n.id);
    const g = svgEl('g', { transform: `translate(${p.x} ${p.y})`, role: 'button', tabindex: '0' }, 'nd');
    g.dataset.st = n.status || 'pending';
    g.dataset.node = n.id;
    if (dagNode && dagNode.run_id === d.run_id && dagNode.node === n.id) g.dataset.sel = '1';
    g.appendChild(svgEl('rect', { width: DAG_W, height: DAG_H, rx: 9 }));
    const mark = dagMark(n.status, 17, DAG_H / 2);
    g.appendChild(mark);
    const id = svgEl('text', { x: 31, y: 19 }, 'id');
    id.textContent = n.id;
    const ag = svgEl('text', { x: 31, y: 32 }, 'ag');
    ag.textContent = n.subagent + (n.instance ? ' @' + n.instance : '');
    // Always present, even while empty: the clock below writes into it every
    // second, and a node that starts running must not have to be redrawn to
    // grow somewhere to put its time.
    const tm = svgEl('text', { x: DAG_W - 11, y: 19, 'text-anchor': 'end' }, 'tm');
    tm.textContent = dagTook(n);
    g.append(id, ag, tm);
    d.els.set(n.id, { g, tm, mark });
    const open = () => dagOpenNode(d.run_id, n);
    g.onclick = open;
    g.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } };
    // The graph is a map, not a table: what a node is and who is running it are
    // in the box, and how long is on the title for the one being pointed at.
    const title = svgEl('title');
    title.textContent = `${n.id} · ${n.subagent}${n.instance ? ' @' + n.instance : ''}`;
    g.appendChild(title);
    svg.appendChild(g);
  });
  return svg;
}

function dagTook(n) {
  if (!n.started_at) return '';
  const end = n.ended_at || Date.now();
  return dur(Math.max(end - n.started_at, 1000));
}

/* ── the clock on a running node ──────────────────────────────────────────
   Only two events ever arrive for a node: it started, and it ended. Between
   them nothing is sent, so a number drawn once sat frozen for exactly the
   interval a reader is watching it for -- a node that took four minutes showed
   "1.0s" for all four of them and then jumped.

   One interval for the whole panel, holding no state of its own: it re-reads the
   graph each second and stops itself as soon as nothing is running, so a
   finished run leaves no timer behind. */
let dagTicker = null;

function dagStopClock() {
  if (dagTicker) { clearInterval(dagTicker); dagTicker = null; }
}

function dagStartClock(d) {
  dagStopClock();
  const anyRunning = () => [...d.nodes.values()].some((n) => n.status === 'running');
  if (!anyRunning()) return;
  dagTicker = setInterval(() => {
    if (!anyRunning()) { dagStopClock(); return; }
    d.nodes.forEach((n) => {
      if (n.status !== 'running') return;
      const slot = d.els && d.els.get(n.id);
      if (slot) slot.tm.textContent = dagTook(n);
    });
  }, 1000);
}

function drawDag() {
  const key = sheetSession();
  sheetDropClass('dsheet', key);
  dagStopClock();
  const d = dagFor(key);
  if (!d) return;

  const sheet = mk('div', 'dsheet');
  sheet.setAttribute('role', 'group');
  sheet.setAttribute('aria-label', T('gui.dag.aria'));
  sheet.dataset.fold = String(!!d.folded);

  const head = mk('div', 'hd');
  head.appendChild(mk('span', 'ttl', T('gui.dag.title')));
  const gist = mk('div', d.done ? 'sum' : 'gist', d.done ? dagSummary(d) : dagGist(d));
  head.appendChild(gist);
  d.gist = gist;

  const fold = mk('button', 'ic tipdn');
  fold.appendChild(ico('M6.5 10 12 15.5 17.5 10', 'cv'));
  const setFold = (v) => {
    d.folded = v;
    sheet.dataset.fold = String(v);
    const lb = T(v ? 'gui.dag.unfold' : 'gui.dag.fold');
    fold.dataset.tip = lb;
    fold.setAttribute('aria-label', lb);
  };
  fold.onclick = () => setFold(!d.folded);
  /* The one writer of the fold, so an in-place update folds through the same
     three writes the button does rather than setting the flag and leaving the
     sheet showing the opposite. */
  d.setFold = setFold;

  const x = mk('button', 'ic tipdn');
  x.appendChild(ico('M7 7l10 10M17 7 7 17'));
  x.dataset.tip = T('gui.dag.close');
  x.setAttribute('aria-label', T('gui.dag.close'));
  x.onclick = () => { DAGS.delete(key); drawDag(); };

  head.append(fold, x);
  sheet.appendChild(head);
  setFold(!!d.folded);

  const canvas = mk('div', 'canvas');
  canvas.appendChild(dagSvg(d));
  sheet.appendChild(canvas);

  sheetAdd(sheet, key);
  /* Text can only be measured once it is rendered, so the id fit runs after
     the sheet is in the document: a long node id used to run under the clock
     in its own corner. The clock's column is reserved whether or not a time
     is showing yet -- a node that starts running must not need a re-fit. */
  sheet.querySelectorAll('.nd .id').forEach((el) => {
    const max = DAG_W - 31 - 40;
    let s = el.textContent;
    while (s.length > 1 && el.getComputedTextLength() > max) {
      s = s.slice(0, -1);
      el.textContent = s + '…';
    }
  });
  d.sheet = sheet;
  dagStartClock(d);
}

/* ── keeping a drawn sheet current, without drawing it again ──────────────
   Every `dag.*` event used to land on drawDag, which drops the sheet and builds
   a new one: a five-node run rebuilt it twelve times, and each rebuild slid it
   back in from the bottom, reset the canvas scroll on a graph wider than the
   pane, and dropped whatever the reader had focused. Nothing about a node
   update needs a new sheet -- the layout is fixed at run_started, since the
   server sends the whole graph before any node runs, and only colours, marks,
   times and the one-line gist move after that. So they move, and the sheet
   stays where it is. Falls back to a full draw when there is no live sheet to
   touch (first event, or a reader who closed and reopened the page). */
const dagLive = (d) => !!(d && d.sheet && d.els && document.body.contains(d.sheet));

function dagTouch(d) {
  if (!dagLive(d)) { drawDag(); return; }
  if (d.setFold && d.sheet.dataset.fold !== String(!!d.folded)) d.setFold(!!d.folded);
  if (d.gist) {
    d.gist.className = d.done ? 'sum' : 'gist';
    d.gist.textContent = d.done ? dagSummary(d) : dagGist(d);
  }
  d.nodes.forEach((n) => {
    const slot = d.els.get(n.id);
    if (!slot) return;
    const st = n.status || 'pending';
    if (slot.g.dataset.st !== st) {
      slot.g.dataset.st = st;
      const next = dagMark(n.status, 17, DAG_H / 2);
      slot.mark.replaceWith(next);
      slot.mark = next;
    }
    slot.tm.textContent = dagTook(n);
  });
  /* Edges darken as their source completes, and that is a class on a path
     rather than a shape, so it can be reapplied without a relayout. */
  const done = new Set();
  d.nodes.forEach((n) => { if (n.status === 'completed') done.add(n.id); });
  d.sheet.querySelectorAll('.edge, .tip').forEach((el) => {
    const from = el.dataset.from;
    if (from) el.classList.toggle('flowed', done.has(from));
  });
  dagStartClock(d);
}

/* The selection is one attribute on one node; moving it does not need the
   graph rebuilt around it. */
function dagSelect(d) {
  if (!dagLive(d)) return;
  d.els.forEach((slot, id) => {
    const on = dagNode && dagNode.run_id === d.run_id && dagNode.node === id;
    if (on) slot.g.dataset.sel = '1';
    else delete slot.g.dataset.sel;
  });
}

function dagSummary(d) {
  const s = d.summary || {};
  const bits = [T('gui.dag.done').replace('{n}', String(s.completed || 0)).replace('{t}', String(s.total || d.order.length))];
  if (s.failed) bits.push(T('gui.dag.failed').replace('{n}', String(s.failed)));
  if (s.skipped) bits.push(T('gui.dag.skipped').replace('{n}', String(s.skipped)));
  return bits.join(' · ');
}

/* A node opens where a sub-agent's work already lives, rather than growing a
   second transcript view inside the sheet: same panel, same renderer, and the
   sheet stays the map rather than becoming the territory. */
function dagOpenNode(runId, n) {
  dagNode = { run_id: runId, node: n.id, agent: n.subagent, label: n.id };
  agentOpen = null;
  if (!wsOpen) setWs(true);
  wsPick('agents');
  drawWs();
  /* The sheet marks the node whose transcript is open. Moving one attribute,
     not rebuilding the graph: every click used to drop the sheet and animate a
     new one in, so picking a second node to compare against the first meant
     watching the first one leave. */
  dagSelect(dagFor());
}
/* The trail's dag card opens a node through the same reader. */
delegOpenNode = (runId, nodeId) => dagOpenNode(runId, { id: nodeId });

/* Per-node status for a card whose events are long gone: `dag.get` reads the
   run back off disk, reconciled against the registry, so a graph reopened from
   history shows what actually happened rather than a row of pending dots. */
delegReadDag = (runId) => rpc.call('dag.get', { run_id: runId, session_key: cur })
  .then((r) => ((r && r.run && r.run.files) || []).map((f) => ({ node: f.node, status: f.status })));

/* "View in workspace" on a spawn row: open the panel on the run's own record,
   not just on the list. The list may not have caught the new run yet, so a
   couple of short retries cover the gap between the call and its row. */
delegOpenSpawn = (agent, label) => {
  setWs(true, 'agents');
  const match = () => AGENTS.find((x) => x.kind !== 'dag'
    && (!label || plainTitle(x.label) === plainTitle(label))
    && (!agent || (x.agent || 'raven') === (agent || 'raven')));
  const attempt = (n) => {
    const it = match();
    if (it) { agentOpenRow(it); return; }
    if (n >= 4) return;
    agentsRefresh(true);
    setTimeout(() => attempt(n + 1), 700);
  };
  attempt(0);
};

/* One dag node's own transcript: what it was asked, what it did on the way, and
   what it answered. Painted by the same function a spawned call's transcript is,
   so the two kinds of delegated work read the same way and only one of them has
   to be kept append-only -- a node polled while it runs used to be torn down and
   rebuilt on every tick, which closed every fold the reader had opened. */
function dagNodePaint(box, it) {
  const row = AGENTS.find((a) => a.kind === 'dag' && a.run_id === it.run_id && a.node === it.node);
  return rpc.call('dag.node', { run_id: it.run_id, node: it.node, session_key: cur })
    .then((r) => {
      if (!document.body.contains(box)) return;
      const n = (r && r.node) || {};
      agentPaint(
        box,
        { messages: n.messages || [], status: row ? row.status : null },
        { key: `dag:${it.run_id}:${it.node}`, empty: T('gui.dag.node_empty') },
      );
      /* Said plainly rather than left to a reader wondering where the rest went:
         the file on disk is whole, this is its head. Once, not once per poll. */
      if (n.output_truncated && !box.querySelector(':scope > .wsnote')) {
        box.appendChild(mk('div', 'wsnote', T('gui.dag.truncated')));
      }
    })
    .catch((e) => {
      if (!document.body.contains(box)) return;
      agentDrawn.key = null;
      box.innerHTML = '';
      box.appendChild(mk('div', 'wsempty', (e && e.message) || String(e)));
    });
}

dagNodeRender = (box, it) => {
  /* A reopened panel draws into a new box, so the paint has to start over even
     though the node it is drawing has not changed. */
  agentDrawn.key = null;
  dagNodePaint(box, it);
};

/* Dev-only hook, beside __clarify and __approve and for the same reason: the
   graph is only reachable by configuring third-party sub-agents and spending a
   multi-agent run, which is too long a loop to design a layout in.
   `window.__dag()` feeds the same three events the server sends. */
window.__dag = (ev) => onEvent(ev && ev.type ? ev : {
  type: 'dag.run_started',
  payload: {
    run_id: '20260812T120000Z-deadbeef',
    nodes: [
      { id: 'survey', subagent: 'Researcher', depends_on: [] },
      { id: 'read_a', subagent: 'Researcher', depends_on: ['survey'] },
      { id: 'read_b', subagent: 'Coder', depends_on: ['survey'] },
      { id: 'read_c', subagent: 'Coder', instance: 'w2', depends_on: ['survey'] },
      { id: 'merge', subagent: 'Writer', depends_on: ['read_a', 'read_b', 'read_c'] },
      { id: 'review', subagent: 'Critic', depends_on: ['merge'] },
    ],
  },
});

})();
