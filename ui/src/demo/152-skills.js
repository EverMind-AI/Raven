/* ══ module 2c: skill market ══════════════════════════════════════
   The renderer is the skills island (ui/src/features/skills/): the hub
   market, the installed view and the shared detail drawer's skill sheet
   all render through it, off DS.skills. What remains here is its shell
   face -- the chrome around #capsBody (title, search field, the
   installed button), the names other layers still call -- and the
   fixture source. */

/* Start a task with the capability already named: a fresh session whose
   composer opens pre-filled, cursor at the end, ready to complete.
   Shared verb: the plugin layer and the skills island both call it. */
function useInTask(promptKey, name) {
  closeDetail();
  $('#newBtn').click();
  const ta = $('#ta');
  ta.value = T(promptKey, { name });
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

/* Placeholder card shown while a hub round-trip is in flight. The island
   draws its own copy for the market grid; this one remains because the
   extensions loader (live layer) builds the caps page's first-open
   skeleton from it. */
function hubSkeleton() {
  const c = mk('div', 'hubcard skel');
  c.setAttribute('aria-hidden', 'true');
  const bar = (w, h, extra) => {
    const b = mk('span', 'sk');
    b.style.cssText = `width:${w};height:${h};${extra || ''}`;
    return b;
  };
  const top = mk('div', 'top');
  const head = mk('div', 'pmhead');
  head.appendChild(bar('34px', '34px', 'border-radius:10px;flex:none'));
  const id = mk('div', 'pmid');
  id.style.cssText = 'display:grid;gap:6px';
  id.append(bar('110px', '12px'), bar('70px', '9px'));
  head.appendChild(id);
  top.appendChild(head);
  c.appendChild(top);
  const one = mk('div');
  one.style.cssText = 'display:grid;gap:7px';
  one.append(bar('100%', '10px'), bar('72%', '10px'));
  c.appendChild(one);
  const foot = mk('div', 'foot');
  foot.append(bar('58px', '22px', 'margin-left:auto;border-radius:8px'));
  c.appendChild(foot);
  return c;
}

/* Mirror of the island's view, read by the plugin layer's hero sync. */
let skView = 'market';

/* The installed entry point rides in the filter bar, exactly like the
   plugin tab's -- created once, shown only on the skill market. */
const skInstBtn = (() => {
  const b = mk('button', 'pminstbtn');
  b.onclick = () => RavenIslands.skills.toggleView();
  $('.cbar').appendChild(b);
  return { sync() {
    b.hidden = extTab !== 'skill' || RavenIslands.skills.view() === 'installed';
    b.innerHTML = '';
    b.append(mk('span', null, T('gui.plug.installed_n', { n: SKILLS.length })));
  } };
})();

/* The skill tab's face on #capsBody: the island owns everything inside the
   box, and the chrome above it is still the page's, set here on every draw.
   demo/153-plugins.js wraps this name for the plugin tab, so a call that
   arrives here is always a skill draw. */
function drawCaps() {
  const box = $('#capsBody'); box.innerHTML = '';
  const title = T('gui.tab.skills');
  const installed = RavenIslands.skills.view() === 'installed';
  $('#capsTitle').textContent = installed ? T('gui.plug.installed_title') : title;
  $('#capsPage').setAttribute('aria-label', title);
  $('#cKind').hidden = true;
  $('#advAdd').hidden = true;
  $('#cq').placeholder = T('gui.hub.search_ph');
  $('.cbar').style.display = installed ? 'none' : '';
  RavenIslands.skills.attach(box);
  RavenIslands.skills.redraw();
  /* The first reveal fetches; a boot-time draw of the closed page must
     not fire a hub search nobody asked for. */
  if ($('#capsPage').dataset.open === 'true') RavenIslands.skills.ensureSearch();
  skInstBtn.sync();
  drawCapsBadge();
}

{
  const prevInput = $('#cq').oninput;
  $('#cq').oninput = () => {
    if (extTab === 'skill') { RavenIslands.skills.setQuery($('#cq').value.trim()); return; }
    if (prevInput) prevInput();
  };
  $('#cq').onkeydown = (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key !== 'Enter' || extTab !== 'skill') return;
    e.preventDefault();
    RavenIslands.skills.searchNow($('#cq').value.trim());
  };

  const prevExtSet = extSet;
  extSet = function (tab) {
    const was = extTab;
    prevExtSet(tab);
    if (extTab !== was) RavenIslands.skills.reset();
  };

  const prevShowPage = showPage;
  showPage = function (id) {
    prevShowPage(id);
    if (id !== 'capsPage') RavenIslands.skills.dropDrawer();
  };

  const prevCloseDetail = closeDetail;
  closeDetail = function () { RavenIslands.skills.dropDrawer(); prevCloseDetail(); };
  $('#dClose').onclick = () => closeDetail();

  /* The island owns the view; the chrome follows it from out here. A view
     flip redraws the whole tab (title, bar, hero) through drawCaps; any
     other change only needs the installed count refreshed. */
  RavenIslands.skills.subscribe(() => {
    const v = RavenIslands.skills.view();
    if (v !== skView) { skView = v; if (extTab === 'skill') drawCaps(); return; }
    skInstBtn.sync();
  });
}

/* The fixture source: a small canned hub, so the market is explorable
   with no gateway behind the page. Search answers copies, letting the
   island mark rows installed without editing the catalogue; install
   grows SKILLS the same way ext.list would. */
const HUB_FIXTURE = [
  { id: 'sh-code-review', name: '代码审查清单', category: 'DEV', source: 'skillhub',
    quality_score: 0.92, tags: ['review', 'quality'],
    description: '按你团队的规矩审代码，而不是通用建议' },
  { id: 'sh-commit-doctor', name: 'commit-message-doctor', category: 'DEV', source: 'skillhub',
    quality_score: 0.88, tags: ['git', 'conventions'],
    description: '把改动整理成规范的提交信息，附带范围与理由' },
  { id: 'sh-sql-style', name: 'SQL 规范', category: 'DATA', source: 'skillhub',
    quality_score: 0.81, tags: ['sql'],
    description: '命名、缩进、禁用写法按你们的库来' },
  { id: 'sh-meeting-notes', name: '会议纪要格式', category: 'WRITING', source: 'skillhub',
    quality_score: 0.86, tags: ['notes'],
    description: '决议、责任人、截止时间三段式' },
  { id: 'sh-compete', name: '竞品调研方法', category: 'PRODUCTIVITY', source: 'skillhub',
    quality_score: 0.74, tags: ['research'],
    description: '一套做市场调研的步骤与输出格式' },
  { id: 'sh-pdf-extract', name: 'pdf-extractor', category: 'DOC-PROC', source: 'github/acme',
    source_url: 'https://example.com/pdf-extractor', quality_score: 0.79, tags: ['pdf', 'ocr'],
    description: '从 PDF 里抽结构化数据，表格也能对齐' },
  { id: 'sh-a11y-audit', name: 'a11y-audit', category: 'FRONTEND-UI', source: 'skillhub',
    quality_score: 0.83, tags: ['accessibility'],
    description: '按 WCAG 走查页面，输出可执行的修复清单' },
  { id: 'sh-incident-rb', name: 'incident-runbook', category: 'DEVOPS-INFRA', source: 'skillhub',
    quality_score: 0.9, tags: ['oncall'],
    description: '事故响应的分级、通报与复盘模板' },
  { id: 'sh-prompt-eval', name: 'prompt-eval', category: 'AI-ML', source: 'skillhub',
    quality_score: 0.77, tags: ['eval'],
    description: '给提示词跑一组回归用例，报告漂移' },
  { id: 'sh-threat-model', name: 'threat-model', category: 'SECURITY', source: 'skillhub',
    quality_score: 0.85, tags: ['security'],
    description: '按 STRIDE 给新功能画威胁模型' },
];

DS.skills ??= {
  search: async ({ query, category, page, limit }) => {
    const q = (query || '').toLowerCase();
    const rows = HUB_FIXTURE.filter((x) => (!category || x.category === category)
      && (!q || `${x.name} ${x.description}`.toLowerCase().includes(q)));
    const from = ((page || 1) - 1) * (limit || 24);
    const items = rows.slice(from, from + (limit || 24)).map((x) => {
      const inst = SKILLS.find((s) => s.name === x.name);
      return { ...x, installed: !!inst, installed_name: inst ? inst.name : '' };
    });
    return { items, total: rows.length };
  },
  detail: async (id) => {
    const x = HUB_FIXTURE.find((h) => h.id === id) || {};
    return { description: x.description, category: x.category, source: x.source,
      license: 'MIT', quality_score: x.quality_score, tags: x.tags || [],
      subscores: { utility: 8, robustness: 7, safety: 9 },
      files: ['SKILL.md'], body_tokens: 1200,
      skill_md: `# ${x.name || id}\n\n${x.description || ''}` };
  },
  install: async (id) => {
    const x = HUB_FIXTURE.find((h) => h.id === id);
    if (!x) return;
    SKILLS.push({ id: x.name, name: x.name, reach: 'local', state: 'on',
      glyph: (x.name[0] || 'S').toUpperCase(), ver: '—', src: x.source,
      one: x.description, cat: x.category, hub: true, hubId: x.id });
  },
  remove: async (name) => {
    const i = SKILLS.findIndex((s) => s.name === name);
    if (i >= 0) SKILLS.splice(i, 1);
  },
  installed: () => SKILLS,
};
