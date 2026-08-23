/* ---- module pages: real data ---------------------------------------
   Skills and plugins still refill the demo arrays their legacy pages read.
   Built-in tools belong only to DS.settings now, so live keeps those rows in
   its own list. Row mutations are intercepted with property setters that
   persist through settings.set. */

const fmt2 = (n) => String(n).padStart(2, '0');
function fmtStamp(ms) {
  if (!ms) return '—';
  const d = new Date(ms), now = new Date();
  const day0 = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const hm = `${fmt2(d.getHours())}:${fmt2(d.getMinutes())}`;
  if (d.getTime() >= day0 && d.getTime() < day0 + 86400000) return T('gui.time.today', { hm });
  if (d.getTime() >= day0 - 86400000 && d.getTime() < day0) return T('gui.time.yesterday', { hm });
  if (d.getTime() >= day0 + 86400000 && d.getTime() < day0 + 2 * 86400000) return T('gui.time.tomorrow', { hm });
  return T('gui.time.md_hm', { m: d.getMonth() + 1, d: d.getDate(), hm });
}
function fmtEvery(ms) {
  if (ms % 3600000 === 0) return T('gui.dur.h', { n: ms / 3600000 });
  if (ms % 60000 === 0) return T('gui.dur.m', { n: ms / 60000 });
  return T('gui.dur.s', { n: Math.round(ms / 1000) });
}

/* -- extensions ----------------------------------------------------- */
// Display names come from the catalogue; an unknown tool keeps its raw id.
const toolLabel = (n) => T('tool.' + n, null, n);
const TOOL_GROUP_OF = (n) => /file|dir|grep|glob|sheet|pdf/.test(n) ? 'file'
  : /web|fetch|search|research|browser/.test(n) ? 'net'
  : /ask|message|clarify/.test(n) ? 'ask' : 'run';
const TOOL_DANGER = new Set(['write_file', 'edit_file', 'exec']);

let disabledToolsLive = [];
let pluginsDisabledLive = [];
let toolsLive = [];

/* The engine re-reads tools.disabledTools once per assembled tool array, so the
   toggle lands on the next turn. Not the same promise as the plugin toggle below:
   plugins.disabled is still read at startup, and its toast still says so. */
function persistDisabledTools() {
  rpc.call('settings.set', { key: 'tools.disabledTools', value: disabledToolsLive })
    .then(() => toast('已保存 · 下一轮对话生效'))
    .catch((e) => toast(`保存失败：${e.message || e}`));
}

function mkToolRow(t) {
  const id = t.name;
  const o = { id, name: toolLabel(id), group: TOOL_GROUP_OF(id),
    reach: TOOL_GROUP_OF(id) === 'net' ? 'net' : 'local',
    danger: TOOL_DANGER.has(id), one: t.description || '',
    /* Present but withheld for want of a key. The row exists so the reader
       learns the tool exists and what it wants -- before this, a key-gated
       tool was simply absent, which reads as removed. */
    needs: t.needs || null };
  if (t.needs) {
    o.on = false;
    return o;
  }
  Object.defineProperty(o, 'on', {
    get: () => !disabledToolsLive.includes(id),
    set: (v) => {
      disabledToolsLive = v ? disabledToolsLive.filter((x) => x !== id) : [...new Set([...disabledToolsLive, id])];
      persistDisabledTools();
    },
  });
  return o;
}

function mkSkillRow(s) {
  const o = { id: s.name, name: s.name, reach: 'local', glyph: (s.name[0] || 'S').toUpperCase(),
    ver: '—', src: s.source, one: s.description || '', cat: s.source, hub: !!s.hub, hubId: s.hub_id || '' };
  Object.defineProperty(o, 'state', {
    get: () => 'on',
    set: () => toast(T('gui.hub.auto_use')),
  });
  return o;
}

function mkPluginRow(p) {
  const id = p.id;
  const o = { id, name: p.display_name || id, reach: 'local', glyph: '◧', ver: p.version,
    src: T(p.bundled ? 'gui.ext.builtin_plugin' : 'gui.ext.plugin'), one: '',
    cat: T('gui.ext.plugin'), tools: [], perms: [] };
  Object.defineProperty(o, 'state', {
    get: () => (pluginsDisabledLive.includes(id) ? 'off' : p.enabled ? 'on' : 'off'),
    set: (v) => {
      pluginsDisabledLive = v === 'on'
        ? pluginsDisabledLive.filter((x) => x !== id)
        : [...new Set([...pluginsDisabledLive, id])];
      rpc.call('settings.set', { key: 'plugins.disabled', value: pluginsDisabledLive })
        .then(() => toast('已保存 · 重启引擎后生效'))
        .catch((e) => toast(`保存失败：${e.message || e}`));
    },
  });
  return o;
}

/* Legacy-state mapping keeps the rail badge (needsAttn counts 'need'/'fail')
   working; the plugin page itself renders from the raw snapshot in o.m. */
const MCP_LEGACY = { connected: 'on', connecting: 'on', auth_required: 'need', error: 'fail', disconnected: 'off' };

function mkMcpRow(m) {
  const o = { id: `mcp:${m.name}`, name: m.name, reach: m.transport === 'stdio' ? 'local' : 'net',
    glyph: '◇', ver: '—', src: `${m.transport} · MCP`, cat: 'MCP', m,
    one: m.connected ? T('gui.ext.mcp_tools', { n: m.tool_count }) : '', tools: [], perms: [] };
  Object.defineProperty(o, 'state', {
    get: () => (!m.enabled ? 'off' : MCP_LEGACY[m.state] || 'off'),
    set: (v) => pmToggle(m.name, v === 'on'),
  });
  return o;
}

async function loadExt() {
  const [ext, cfg] = await Promise.all([rpc.call('ext.list', {}), rpc.call('settings.get', {})]);
  const raw = cfg.settings || {};
  disabledToolsLive = (raw.tools && raw.tools.disabledTools) || [];
  pluginsDisabledLive = (raw.plugins && raw.plugins.disabled) || [];
  toolsLive = ext.tools.filter((t) => !t.mcp_server).map(mkToolRow);
  SKILLS.length = 0;
  ext.skills.forEach((s) => SKILLS.push(mkSkillRow(s)));
  PLUGINS.length = 0;
  ext.plugins.forEach((p) => PLUGINS.push(mkPluginRow(p)));
  ext.mcp.forEach((m) => PLUGINS.push(mkMcpRow(m)));
}

let extLoaded = false;
openCaps = async function (tab) {
  /* Page first, data second: the old await-first shape left the previous
     tab's cards on screen until the RPC returned -- the "stale flash". */
  extSet(tab);
  showPage('capsPage');
  if (extLoaded) { drawCaps(); drawCapsBadge(); }
  else {
    const box = $('#capsBody'); box.innerHTML = '';
    const g = mk('div', 'hubgrid');
    for (let i = 0; i < 6; i++) g.appendChild(hubSkeleton());
    box.appendChild(g);
  }
  try { await loadExt(); extLoaded = true; } catch (e) { toast(`加载失败：${e.message || e}`); }
  drawCaps(); drawCapsBadge();
};
