/* ══ module 2d: plugin market ═════════════════════════════════════
   The renderer is the plugins island (ui/src/features/plugins/); what
   remains here is its shell face -- the tab chrome around #capsBody, the
   names other layers still call, and the fixture source. The island owns
   everything drawn inside the body and the shared detail drawer.

   This part must load AFTER 152-skills.js: its drawCaps wrapper is the
   outermost of the chain, mirroring the old live-layer order -- the skill
   branch below hands over to the skills wrapper, and the shared page hero
   is synced from here for both tabs. */

// Stable per-name hue: same plugin, same colour, every render and page.
// Kept as a shell helper because the memory drawer uses it too.
function pmTile(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  const t = mk('span', 'pmtile th' + (h % 8));
  t.textContent = (name[0] || '?').toUpperCase();
  return t;
}

/* The live extensions loader flips MCP rows through this name. */
function pmToggle(name, on) { RavenIslands.plugins.toggleMcp(name, on); }

/* The "installed" entry point rides in the filter bar, like the skills view
   switch — created once, shown only on the plugin tab. */
const pmInstBtn = (() => {
  const b = mk('button', 'pminstbtn');
  b.onclick = () => { RavenIslands.plugins.toggleView(); };
  $('.cbar').appendChild(b);
  return { el: b, sync() {
    b.hidden = extTab !== 'plugin' || RavenIslands.plugins.view() === 'installed';
    const n = RavenIslands.plugins.installedCount();
    const attn = attnCount();
    b.innerHTML = '';
    b.append(mk('span', null, T('gui.plug.installed_n', { n })));
    if (attn) b.appendChild(mk('span', 'pmbdg', String(attn)));
  } };
})();

/* The plugin tab's face on #capsBody: chrome first, then the island host.
   The host node survives other tabs clearing #capsBody (they only detach
   it), so re-appending it costs nothing and loses no React state. */
function drawPlugTab() {
  const box = $('#capsBody');
  box.innerHTML = '';
  box.appendChild(RavenIslands.plugins.host);
  const title = T('gui.tab.plugins');
  const view = RavenIslands.plugins.view();
  $('#capsTitle').textContent = view === 'installed' ? T('gui.plug.installed_title') : title;
  $('#capsPage').setAttribute('aria-label', title);
  $('#cKind').hidden = true;
  $('#advAdd').hidden = view !== 'market';
  $('#cq').placeholder = T('gui.plug.search_ph');
  $('.cbar').style.display = view === 'installed' ? 'none' : '';
  pmInstBtn.sync();
  RavenIslands.plugins.redraw();
  /* The first reveal fetches; a boot-time draw of the closed page must
     not fire a market search nobody asked for. */
  if ($('#capsPage').dataset.open === 'true') RavenIslands.plugins.searchIfIdle();
}

{
  /* The hero sits above the search bar, so it lives outside #capsBody --
     one node, repopulated on every draw for whichever view is up. It
     covers both tabs, which is why it rides on this outermost wrapper:
     skView is the mirror demo/152-skills.js maintains. */
  const pageHero = () => {
    let h = $('#pageHero');
    if (!h) {
      h = mk('div', 'pmhero');
      h.id = 'pageHero';
      const bar = document.querySelector('#capsPage .cbar');
      bar.parentNode.insertBefore(h, bar);
    }
    return h;
  };
  /* Title only — no tagline under it; that copy read as marketing, not UI. */
  const syncHero = () => {
    const h = pageHero();
    h.innerHTML = '';
    let title = '';
    if (extTab === 'plugin' && RavenIslands.plugins.view() === 'market') title = T('gui.plug.hero');
    else if (extTab === 'skill' && skView === 'market') title = T('gui.hub.hero');
    h.hidden = !title;
    if (title) h.appendChild(mk('h3', null, title));
  };

  const prevDrawCaps = drawCaps;
  drawCaps = function () {
    if (extTab === 'plugin') {
      skInstBtn.sync();  // the skills "installed" button must not linger on this tab
      drawPlugTab();
      syncHero();
      return;
    }
    // Undo this tab's chrome before handing back: the plugin view hid the
    // status pills, and the skill view re-hides them for itself.
    $('#cKind').hidden = false;
    $('.cbar').style.display = '';
    prevDrawCaps();
    pmInstBtn.sync();
    syncHero();
  };

  const prevExtSet = extSet;
  extSet = function (tab) {
    const was = extTab;
    prevExtSet(tab);
    if (extTab !== was) { RavenIslands.plugins.reset(); $('.cbar').style.display = ''; }
  };

  const prevShowPage = showPage;
  showPage = function (id) {
    prevShowPage(id);
    if (id !== 'capsPage') { RavenIslands.plugins.drawerClosed(); $('.cbar').style.display = ''; }
  };

  const prevCloseDetail = closeDetail;
  closeDetail = function () { RavenIslands.plugins.drawerClosed(); prevCloseDetail(); };
  // The X button captured the previous closeDetail reference at bind time.
  $('#dClose').onclick = () => closeDetail();

  const prevInput = $('#cq').oninput;
  $('#cq').oninput = () => {
    if (extTab === 'plugin') { RavenIslands.plugins.setQuery($('#cq').value.trim()); return; }
    if (prevInput) prevInput();
  };
}

/* The fixture source: a canned catalog behind the same interface the rpc
   source implements. Registered, not declared-for-override -- live mode
   installs its own DS.plugins and this object is never consulted. */
DS.plugins ??= (() => {
  const CATS = ['developer', 'productivity', 'data'];
  const MARKET = [
    { id: 'github-mcp', name: 'GitHub', publisher: 'github.com', verified: true,
      summary: '读 issue 与 PR，提交评论', category: 'developer',
      tool_preview_count: 3, risk_tier: 1, installed: false,
      version: '1.4.0', homepage: 'https://github.com/mcp',
      description: '连上 GitHub 的官方 MCP 服务，读写 issue、PR 与评论。',
      contributes: [{ kind: 'mcp',
        connection: { type: 'http', url: 'https://api.githubcopilot.com/mcp' },
        auth: { mode: 'oauth' },
        tools_preview: ['list_issues', 'get_pr', 'create_comment'] }] },
    { id: 'websearch', name: '网页搜索', publisher: 'serper.dev', verified: true,
      summary: '让 Raven 查得到网上的实时信息', category: 'data',
      tool_preview_count: 2, risk_tier: 1, installed: false,
      version: '0.9.2', homepage: 'https://serper.dev',
      description: '接入 serper.dev 的搜索接口，Raven 可以自己找资料。',
      contributes: [{ kind: 'mcp',
        connection: { type: 'http', url: 'https://mcp.serper.dev' },
        auth: { mode: 'apikey', fields: [{ key: 'api_key', label: 'API Key', secret: true, help_url: 'https://serper.dev' }] },
        tools_preview: ['web_search', 'web_news'] }] },
    { id: 'sqlite', name: 'SQLite', publisher: 'raven-tools', verified: false,
      summary: '在本机查询与修改 SQLite 数据库', category: 'data',
      tool_preview_count: 2, skill_count: 1, risk_tier: 2, installed: false,
      version: '0.3.1', homepage: '',
      description: '本地运行的 stdio 服务，直接读写你机器上的数据库文件。',
      contributes: [
        { kind: 'mcp', connection: { command: 'npx', args: ['-y', '@raven/sqlite-mcp'] },
          auth: { mode: 'none' }, tools_preview: ['query', 'execute'] },
        { kind: 'skill', name: 'sql-review', skillhub_id: 'sql-review' },
      ] },
    { id: 'notion', name: 'Notion', publisher: 'notion.so', verified: true,
      summary: '把结果写进你的 Notion 库', category: 'productivity',
      tool_preview_count: 3, risk_tier: 1, installed: false,
      version: '2.1.0', homepage: 'https://notion.so/mcp',
      description: '开启后 Raven 可以直接建页面，建议先确认目标库。',
      contributes: [{ kind: 'mcp',
        connection: { type: 'http', url: 'https://mcp.notion.com' },
        auth: { mode: 'oauth' },
        tools_preview: ['search_pages', 'create_page', 'update_page'] }] },
  ];
  const find = (id) => MARKET.find((x) => x.id === id);
  const entryOf = (it) => ({ id: it.id, name: it.name, version: it.version,
    summary: it.summary, description: it.description, homepage: it.homepage,
    publisher: { name: it.publisher, verified: it.verified },
    contributes: it.contributes });
  // The demo's installed shelf: the canned plugin rows plus whatever the
  // reader installs from the canned market during the session.
  const pyRows = PLUGINS.filter((p) => p.state === 'on' || p.state === 'off')
    .map((p) => ({ id: p.id, name: p.name, src: p.src, ver: p.ver, state: p.state }));
  const mcpRows = [];
  return {
    search: async (q, category) => ({
      items: MARKET.filter((x) => (!category || x.category === category)
        && (!q || (x.name + (x.summary || '')).toLowerCase().includes(q.toLowerCase()))),
      categories: CATS,
    }),
    detail: async (id) => ({ entry: entryOf(find(id)), installed: !!find(id).installed }),
    install: async (id) => {
      const it = find(id);
      it.installed = true;
      const mcp = { name: id, enabled: true, state: 'connected', transport: 'http',
        tool_count: (it.contributes[0].tools_preview || []).length };
      mcpRows.push({ id: 'mcp:' + id, name: it.name, m: mcp });
      return { mcp };
    },
    remove: async (name) => {
      const it = find(name);
      if (it) it.installed = false;
      const i = mcpRows.findIndex((r) => r.m.name === name);
      if (i >= 0) mcpRows.splice(i, 1);
    },
    toggle: async (name, enabled) => {
      const r = mcpRows.find((x) => x.m.name === name);
      if (r) r.m.enabled = enabled;
      return r ? r.m : null;
    },
    togglePy: async (row, on) => { row.state = on ? 'on' : 'off'; },
    auth: async () => null,
    rows: () => pyRows.concat(mcpRows),
    reload: async () => {},
  };
})();
