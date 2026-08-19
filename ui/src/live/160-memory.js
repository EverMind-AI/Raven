/* -- data & memory ----------------------------------------------------
   Four EverOS memory kinds behind one page: a stat band that doubles as
   the kind switch, a semantic search box, and a detail drawer carrying
   the one mutation memory supports today (delete, two-click armed).
   Reads go through memory.stats / memory.list; the page never talks to
   EverOS directly. */

const MEM_KINDS = [
  { kind: 'episode', tab: 'gui.mem.tab_episode', hint: 'gui.mem.hint_episode', stat: 'episodes' },
  { kind: 'profile', tab: 'gui.mem.tab_profile', hint: 'gui.mem.hint_profile', stat: 'profiles' },
  { kind: 'agent_case', tab: 'gui.mem.tab_case', hint: 'gui.mem.hint_case', stat: 'agent_cases' },
  { kind: 'agent_skill', tab: 'gui.mem.tab_skill', hint: 'gui.mem.hint_skill', stat: 'agent_skills' },
];
const MEM_PAGE_SIZE = 20;
let memKind = 'episode', memPageNo = 1, memQ = '', memItems = [], memTotal = 0;
let memStats = null, memState = 'idle', memErr = '', memDebounce = null, memBusy = false;

function memWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const two = (n) => String(n).padStart(2, '0');
  return LANG === 'zh'
    ? `${d.getMonth() + 1}月${d.getDate()}日 ${two(d.getHours())}:${two(d.getMinutes())}`
    : `${d.toLocaleString('en-US', { month: 'short' })} ${d.getDate()} ${two(d.getHours())}:${two(d.getMinutes())}`;
}

const memPct = (v) => (v == null ? '' : v <= 1 ? `${Math.round(v * 100)}%` : String(v));

function memMeter(v) {
  const m = mk('span', 'mmeter');
  const bar = mk('i');
  bar.style.width = `${Math.round(Math.min(1, Math.max(0, Number(v) || 0)) * 100)}%`;
  m.appendChild(bar);
  return m;
}

async function memLoadStats() {
  try { memStats = await rpc.call('memory.stats', {}); } catch { memStats = null; }
}

async function memLoad() {
  memState = 'loading';
  memErr = '';
  try {
    const r = await rpc.call('memory.list', {
      kind: memKind, page: memPageNo, page_size: MEM_PAGE_SIZE, q: memQ || null,
    });
    memItems = r.items || [];
    memTotal = r.total || 0;
    memState = 'ready';
  } catch (e) {
    memState = 'error';
    memErr = (e.data && e.data.detail) || e.message || String(e);
  }
  drawMem();
}

openMem = async function () {
  showPage('memPage');
  drawMem();
  memLoadStats().then(drawMem);
  memLoad();
};

function memArm(btn, fn) {
  let armed = false;
  const label = btn.textContent;
  btn.onclick = (e) => {
    e.stopPropagation();
    if (!armed) {
      armed = true;
      btn.textContent = T('gui.mem.confirm_del');
      btn.classList.add('danger');
      setTimeout(() => { armed = false; btn.textContent = label; btn.classList.remove('danger'); }, 4000);
      return;
    }
    fn();
  };
}

function memDelete(it) {
  if (memBusy) return;
  memBusy = true;
  rpc.call('memory.delete', { kind: it.kind, id: it.id })
    .then(() => {
      toast(T('gui.mem.deleted'));
      closeDetail();
      memLoadStats().then(drawMem);
      return memLoad();
    })
    .catch((e) => toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e })))
    .finally(() => { memBusy = false; });
}

function memOpenDetail(it) {
  const body = $('#dBody'); body.innerHTML = '';
  const kindDef = MEM_KINDS.find((k) => k.kind === it.kind);
  const title = it.subject || T(kindDef.tab);
  $('#dTitle').textContent = '';

  // Same drawer opener as the plugin / skill pages: tile, bold name, meta line.
  const head = mk('div', 'pmdhead');
  head.appendChild(pmTile(title));
  const hm = mk('div', 'pmdmeta');
  const l1 = mk('div', 'l1');
  l1.appendChild(mk('b', null, title));
  hm.appendChild(l1);
  hm.appendChild(mk('div', 'l2', [T(kindDef.tab), memWhen(it.timestamp)].filter(Boolean).join(' · ')));
  head.appendChild(hm);
  body.appendChild(head);

  const metaBox = mk('div');
  const mrow = (k, v) => {
    if (!v) return;
    metaBox.appendChild(mk('div', 'pnote', `${k} · ${v}`));
  };
  mrow(T('gui.mem.meta_session'), it.session_id);
  if (it.quality_score != null) mrow(T('gui.mem.meta_quality'), memPct(it.quality_score));
  if (it.confidence != null) mrow(T('gui.mem.meta_confidence'), memPct(it.confidence));
  if (it.maturity_score != null) mrow(T('gui.mem.meta_maturity'), memPct(it.maturity_score));
  if (metaBox.children.length) body.appendChild(metaBox);

  const sec = (label, el) => {
    const s = mk('div', 'pmsec');
    s.appendChild(mk('div', 'cap', label));
    s.appendChild(el);
    body.appendChild(s);
  };
  if (it.kind === 'agent_case') {
    if (it.body) sec(T('gui.mem.sec_approach'), mk('div', 'mempre', it.body));
    if (it.key_insight) sec(T('gui.mem.sec_insight'), mk('div', 'mempre', it.key_insight));
  } else {
    sec(T('gui.mem.sec_detail'), mk('div', 'mempre', it.body || it.summary || ''));
  }

  const delWrap = mk('div', 'pmsec');
  const del = mk('button', 'mini ghost', T('gui.mem.delete'));
  memArm(del, () => memDelete(it));
  delWrap.appendChild(del);
  if (it.kind === 'episode') delWrap.appendChild(mk('div', 'memnote', T('gui.mem.del_episode_note')));
  body.appendChild(delWrap);
  $('#detail').dataset.open = 'true';
}

function memRow(it) {
  const r = mk('div', 'memrow');
  r.tabIndex = 0;
  r.setAttribute('role', 'button');
  const t = mk('div', 't');
  t.appendChild(mk('b', null, it.subject || it.summary || it.id));
  if (it.timestamp) t.appendChild(mk('span', 'when', memWhen(it.timestamp)));
  r.appendChild(t);
  const sub = it.kind === 'agent_case' ? (it.key_insight || it.body) : (it.summary || it.body);
  if (sub) r.appendChild(mk('div', 's', sub));
  const meta = mk('div', 'meta');
  if (typeof it.score === 'number') meta.appendChild(mk('span', 'pmsign faint', it.score.toFixed(2)));
  if (it.kind === 'agent_skill') {
    meta.appendChild(mk('span', 'pmcnt', T('gui.mem.meta_confidence')));
    meta.appendChild(memMeter(it.confidence));
    meta.appendChild(mk('span', 'pmcnt', T('gui.mem.meta_maturity')));
    meta.appendChild(memMeter(it.maturity_score));
  }
  if (it.kind === 'agent_case' && it.quality_score != null) {
    meta.appendChild(mk('span', 'pmsign faint', `${T('gui.mem.meta_quality')} ${memPct(it.quality_score)}`));
  }
  if (meta.children.length) r.appendChild(meta);
  r.onclick = () => memOpenDetail(it);
  r.onkeydown = (e) => { if (e.key === 'Enter') memOpenDetail(it); };
  return r;
}

/* profile_data values are engine-shaped: strings, arrays of objects,
   nested dicts, epoch stamps. Render all of them as prose lines. */
function memVal(v) {
  if (v == null) return '';
  if (Array.isArray(v)) return v.map(memVal).filter(Boolean).join('\n');
  if (typeof v === 'object') {
    return Object.entries(v)
      .map(([k, x]) => `${k}: ${memVal(x)}`)
      .join(' · ');
  }
  return String(v);
}

function memProfileCard(it) {
  const wrap = mk('div');
  const kv = mk('div', 'memkv');
  const data = it.profile_data || {};
  Object.keys(data).forEach((k) => {
    const row = mk('div', 'row');
    row.appendChild(mk('div', 'k', k));
    const isStamp = /_ms$/i.test(k) && Number(data[k]) > 1e12;
    row.appendChild(mk('div', 'v', isStamp ? memWhen(new Date(Number(data[k])).toISOString()) : memVal(data[k])));
    kv.appendChild(row);
  });
  wrap.appendChild(kv);
  const del = mk('button', 'mini ghost', T('gui.mem.delete'));
  del.style.marginTop = '14px';
  memArm(del, () => memDelete(it));
  wrap.appendChild(del);
  wrap.appendChild(mk('div', 'memnote', T('gui.mem.del_profile_note')));
  return wrap;
}

function memPager() {
  const pages = Math.max(1, Math.ceil(memTotal / MEM_PAGE_SIZE));
  if (memQ || pages <= 1) return null;
  const bar = mk('div', 'hubpage');
  const prev = mk('button', 'mini ghost', T('gui.mem.prev'));
  prev.disabled = memPageNo <= 1;
  prev.onclick = () => { memPageNo -= 1; memLoad(); };
  const next = mk('button', 'mini ghost', T('gui.mem.next'));
  next.disabled = memPageNo >= pages;
  next.onclick = () => { memPageNo += 1; memLoad(); };
  bar.append(prev, mk('span', 'pnote', T('gui.mem.page', { p: memPageNo, n: pages })), next);
  return bar;
}

drawMem = function () {
  const box = $('#memBody');
  // A redraw mid-typing must not eat the search field's focus.
  const refocus = document.activeElement && box.contains(document.activeElement)
    && document.activeElement.tagName === 'INPUT';
  box.innerHTML = '';

  const hero = mk('div', 'pmhero');
  hero.appendChild(mk('h3', null, T('gui.mem.hero')));
  box.appendChild(hero);

  const band = mk('div', 'memstats');
  MEM_KINDS.forEach((k) => {
    const b = mk('button', 'mstat');
    b.setAttribute('aria-pressed', String(memKind === k.kind));
    b.appendChild(mk('div', 'k', T(k.tab)));
    b.appendChild(mk('div', 'v', memStats ? String(memStats[k.stat]) : '—'));
    b.appendChild(mk('div', 'h', T(k.hint)));
    b.onclick = () => {
      if (memKind === k.kind) return;
      memKind = k.kind; memPageNo = 1; memQ = ''; memItems = [];
      closeDetail();
      drawMem();
      memLoad();
    };
    band.appendChild(b);
  });
  box.appendChild(band);

  let inp = null;
  if (memKind !== 'profile') {
    const tools = mk('div', 'memtools');
    const find = mk('div', 'cfind');
    find.innerHTML = '<svg class="ic" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
      + 'stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4.3-4.3"/></svg>';
    inp = document.createElement('input');
    inp.placeholder = T('gui.mem.search_ph');
    inp.value = memQ;
    inp.oninput = () => {
      memQ = inp.value.trim();
      clearTimeout(memDebounce);
      memDebounce = setTimeout(() => { memPageNo = 1; memLoad(); }, 350);
    };
    find.appendChild(inp);
    tools.appendChild(find);
    if (memState === 'ready') {
      tools.appendChild(mk('span', 'n', T(memQ ? 'gui.mem.n_hits' : 'gui.mem.n_total', { n: memTotal })));
    }
    box.appendChild(tools);
  }
  if (refocus && inp) {
    inp.focus();
    inp.setSelectionRange(inp.value.length, inp.value.length);
  }

  if (memState === 'error') {
    box.appendChild(mk('div', 'errline-lite', `${T('gui.mem.down')} · ${memErr}`));
    const retry = mk('button', 'mini ghost', T('gui.plug.retry'));
    retry.onclick = memLoad;
    box.appendChild(retry);
    return;
  }
  if (memState !== 'ready' && !memItems.length) {
    box.appendChild(mk('div', 'empty-note', T('gui.hub.reading')));
    return;
  }
  if (!memItems.length) {
    box.appendChild(mk('div', 'empty-note', memQ ? T('gui.mem.none_found', { q: memQ }) : T('gui.mem.empty')));
    return;
  }

  if (memKind === 'profile') {
    box.appendChild(memProfileCard(memItems[0]));
  } else {
    const list = mk('div', 'memlist');
    memItems.forEach((it) => list.appendChild(memRow(it)));
    box.appendChild(list);
    const pager = memPager();
    if (pager) box.appendChild(pager);
  }
};

