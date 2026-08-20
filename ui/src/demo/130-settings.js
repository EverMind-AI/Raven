/* ══ module 3: settings page ══════════════════════════════════════ */
const PROVIDERS = [
  { id:'minimax', name:'MiniMax', models:['minimax-m3','minimax-m2'], on:true, key:'sk-live-9f2c............' },
  { id:'anthropic', name:'Anthropic', models:['claude-opus-4-5','claude-sonnet-4-6'], on:true, key:'sk-ant-4b71............' },
  { id:'openai', name:'OpenAI', models:['gpt-5.1','gpt-5-mini'], on:false, key:'' },
  { id:'deepseek', name:'DeepSeek', models:['deepseek-v3.2'], on:false, key:'' }
];

/* Settings is grouped, not one flat strip. The page count went from 7 to 14,
   and 14 siblings in a row are unnavigable; the groups answer "what am I
   changing" -- myself, the agent, or the machine it runs on. */
const SET_GROUPS = [
  { key: 'gui.set.grp.me', pages: [
    ['usage', 'gui.set.pg.usage'],
    ['look', 'gui.set.pg.look'], ['notify', 'gui.set.pg.notify'],
    ['keys', 'gui.set.pg.keys'], ['about', 'gui.set.pg.about']] },
  { key: 'gui.set.grp.agent', pages: [
    ['model', 'gui.set.pg.model'], ['perm', 'gui.set.pg.perm'],
    ['toolset', 'gui.set.pg.toolset'],
    ['memory', 'gui.set.pg.memory'], ['proact', 'gui.set.pg.proact']] },
  { key: 'gui.set.grp.env', pages: [
    ['exec', 'gui.set.pg.exec'],
    ['channel', 'gui.set.pg.channel'], ['data', 'gui.set.pg.data']] }
];
const SET_TITLE = {};
SET_GROUPS.forEach((g) => g.pages.forEach(([id, key]) => { SET_TITLE[id] = key; }));
let sTab = 'usage';
let mdlAdv = false;

const saved = (what) => toast(T('gui.saved', { what }));
// Filled in from system.version once the socket is up, and unknown until then:
// the running install is the only thing that knows its version, so a literal here
// would be a second source of truth that goes stale at every release. The rail
// foot and the About card both render this as "--" rather than as a guess.
let APP_VERSION = null;
let CONFIG_PATH = '~/.raven/config.json';
let SHOW_TOKENS = true;

/* Every displayed value is read through V(), so each page is written once:
   the demo answers with the fallback, live.js overrides V to read the config
   it pulled over settings.get. */
let V = (path, fallback) => fallback;

/* Real writes that only the live layer can perform. The demo keeps a local
   effect so the shell stays explorable; live.js swaps in the RPC. */
let deleteAllSessions = () => {
  SESS = []; cur = null; drawList(); $('#stage').innerHTML = '';
  $('#title').textContent = T('gui.new_task'); pitch();
};
let checkUpdate = () => notLive();

/* ---- the three tiers ------------------------------------------------
   A control either writes through, or it is tagged and refuses. Nothing sits
   in between: a switch that stays flipped after a change nothing stored is
   what this rework exists to kill.

   A tagged switch or segment never renders the new value to begin with -- both
   read their state from the last draw -- so the only thing to undo is text the
   reader typed. The refusal is spoken in the row, not in a toast: the live
   layer routes toasts to the console, where nobody is looking. */
function nlSay(el, reset, msg) {
  if (reset) reset();
  /* The row when the control sits in one, else the card: a chooser is a whole
     card wide, and the refusal has to land where the click did. */
  const host = el && el.closest ? el.closest('.crow') || el.closest('.scard') : null;
  const text = T(msg || 'gui.set.not_live');
  if (!host) { toast(text); return; }
  host.querySelectorAll('.nlmsg').forEach((n) => n.remove());
  const m = mk('div', 'nlmsg', text);
  host.appendChild(m);
  setTimeout(() => m.remove(), 3600);
}
const notLive = () => toast(T('gui.set.not_live'));
const deadSwi = (on, label) => { const s = swi(on, () => nlSay(s), label); return s; };
const deadText = (val, ph) => {
  const i = textField(val, () => nlSay(i, () => { i.value = val || ''; }), ph);
  return i;
};
const deadBtn = (label) => {
  const b = mk('button', 'mini ghost', label);
  b.onclick = () => nlSay(b);
  return b;
};
const deadSlider = (min, max, step, val, fmt) => {
  const w = slider(min, max, step, val, fmt, () => nlSay(w, () => {
    w.querySelector('input').value = val;
    w.querySelector('.val').textContent = fmt(val);
  }));
  return w;
};
/* Copy says so on itself: the same reason nlSay does not use a toast. */
function cpBtn(label, text) {
  const b = mk('button', 'mini ghost', label);
  b.onclick = () => {
    if (navigator.clipboard) navigator.clipboard.writeText(String(text));
    b.textContent = T('gui.set.copied');
    setTimeout(() => { b.textContent = label; }, 1600);
  };
  return b;
}
const roVal = (text, dim) => mk('span', 'rov' + (dim ? ' dim' : ''), text || T('gui.set.unset'));

/* ---- write-through controls -----------------------------------------
   live.js points settingsWrite at the settings.set RPC; in the demo it
   stays null and every control below falls back to the tagged refusal.
   Vnull is V without the null-means-fallback collapse: memory.backend
   stores null as a real value ("plugin memory off"), and V would paint
   the schema default over it. */
let settingsWrite = null;
let Vnull = (path, fallback) => fallback;
/* EverOS model roles: live.js fills EVEROS over settings.everos and points
   everosWrite at settings.everosSet; in the demo both stay null/dead. */
let EVEROS = null;
let everosWrite = null;
let memEdit = null;
/* API usage stats: live.js fills USAGE over settings.usage on first view. */
let USAGE = null;
let usageLoad = null;

function wSwi(key, on, label) {
  const s = swi(on, (v) => {
    if (!settingsWrite) { nlSay(s); return; }
    settingsWrite(key, v, s);
  }, label);
  return s;
}
function wPick(key, opts, val, map) {
  return pick(opts, val, (v) => {
    if (!settingsWrite) { nlSay(null); return; }
    settingsWrite(key, map ? map(v) : v);
  });
}
function wText(key, val, ph, norm) {
  const i = textField(val, (v) => {
    if (!settingsWrite) { nlSay(i, () => { i.value = val || ''; }); return; }
    const out = norm ? norm(v) : v;
    if (out === undefined) { i.value = val || ''; return; }
    settingsWrite(key, out, i);
  }, ph);
  return i;
}
function wNum(key, val, min, max) {
  const i = textField(String(val), (v) => {
    const n = Number(v);
    if (!Number.isInteger(n) || n < min || n > max) { i.value = String(val); return; }
    if (!settingsWrite) { nlSay(i, () => { i.value = String(val); }); return; }
    settingsWrite(key, n, i);
  }, '');
  i.style.width = '90px';
  i.inputMode = 'numeric';
  return i;
}

/* ---- settings building blocks --------------------------------------
   A module is a few cards, each designed around what it actually is: a
   chooser whose options need a sentence, a set of numbers, a mirror of
   config.json, or a module that is off and should say what it would do. */
function scard(p, title, desc) {
  const c = mk('div', 'scard');
  if (title || desc) {
    const h = mk('div', 'ch');
    if (title) h.appendChild(mk('div', 't', title));
    if (desc) h.appendChild(mk('div', 'd', desc));
    c.appendChild(h);
  }
  p.appendChild(c);
  return c;
}
/* One footnote per card instead of a badge per row. */
function snote(c, text, code) {
  const n = mk('div', 'snote');
  n.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/>'
    + '<path d="M12 11.5v5M12 8h.01"/></svg>';
  const s = mk('span', null, text);
  if (code) { s.appendChild(document.createTextNode(' ')); s.appendChild(mk('code', null, code)); }
  n.appendChild(s);
  c.appendChild(n);
  return n;
}
function crow(c, label, ctl, hint) {
  const r = mk('div', 'crow');
  r.appendChild(mk('div', 'k', label));
  if (hint) r.appendChild(mk('div', 'h', hint));
  const w = mk('div', 'c'); w.appendChild(ctl); r.appendChild(w);
  c.appendChild(r);
  return r;
}
const TICK = '<svg class="tick" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7"/></svg>';
/* opts: [value, name, why]. `fn` null means the choice is explanatory only.
   Options that need no sentence render as a compact segment: a card grid
   with nothing to say per card is just empty space stacked N high. */
function pick(opts, val, fn, cls) {
  if (!cls && opts.every((o) => !o[2])) {
    const w = mk('div', 'sgm');
    opts.forEach(([v, name]) => {
      const b = mk('button', 'pk', name);
      b.setAttribute('aria-pressed', String(v === val));
      b.onclick = () => (fn ? fn(v) : nlSay(w));
      w.appendChild(b);
    });
    return w;
  }
  const w = mk('div', 'spick n' + opts.length + (cls ? ' ' + cls : ''));
  opts.forEach(([v, name, why]) => {
    const b = mk('button', 'pk');
    b.setAttribute('aria-pressed', String(v === val));
    const n = mk('div', 'n', name);
    n.insertAdjacentHTML('beforeend', TICK);
    b.appendChild(n);
    if (why) b.appendChild(mk('div', 'w', why));
    b.onclick = () => (fn ? fn(v) : nlSay(w));
    w.appendChild(b);
  });
  return w;
}
function statTiles(rows) {
  const w = mk('div', 'stats');
  rows.forEach(([v, k]) => {
    const t = mk('div', 'stat');
    t.append(mk('div', 'v' + (v == null ? ' none' : ''), v == null ? T('gui.set.nodata') : String(v)),
      mk('div', 'k', k));
    w.appendChild(t);
  });
  return w;
}
/* rows: [label, value, kind]. kind '' | 'unset' | 'ok' */
function kvList(rows) {
  const w = mk('div', 'skv');
  rows.forEach(([k, v, kind]) => {
    const r = mk('div', 'r');
    r.append(mk('span', 'k', k), mk('span', 'v' + (kind ? ' ' + kind : ''), v));
    w.appendChild(r);
  });
  return w;
}
/* The theme option is a miniature of the theme: three bars on the right ground
   read as "this is what the window will look like" faster than the word does. */
function themePick() {
  const w = mk('div', 'spick n3 thpick');
  const shot = (kind) => {
    const s = mk('div', 'shot ' + kind);
    s.appendChild(mk('div', 'r1'));
    const r2 = mk('div', 'r2');
    r2.append(mk('div', 'bar hd w45'), mk('div', 'bar w70'), mk('div', 'bar w45'));
    s.appendChild(r2);
    return s;
  };
  [['system', T('gui.set.theme_system')], ['light', T('gui.set.theme_light')],
    ['dark', T('gui.set.theme_dark')]].forEach(([v, name]) => {
    const b = mk('button', 'pk');
    b.setAttribute('aria-pressed', String(v === CFG.theme));
    if (v === 'system') {
      const g = mk('div', 'sysgrid');
      g.append(shot('lt'), shot('dk'));
      b.appendChild(g);
    } else {
      b.appendChild(shot(v === 'light' ? 'lt' : 'dk'));
    }
    const lb = mk('div', 'lb', name);
    lb.insertAdjacentHTML('beforeend', TICK);
    b.appendChild(lb);
    b.onclick = () => { CFG.theme = v; applyLook(); lookSave(); drawSettings(); };
    w.appendChild(b);
  });
  return w;
}
/* A font is chosen by looking at it, so the option renders in the face it sets
   -- the families are named in the stack, not only in the label. */
function fontPick() {
  const w = mk('div', 'spick n3 fpick');
  [['system', T('gui.set.codefont_system'), 'ui-monospace, monospace'],
    ['jet', 'JetBrains Mono', '"JetBrains Mono", ui-monospace, monospace'],
    ['sf', 'SF Mono', '"SF Mono", "SFMono-Regular", ui-monospace, monospace']]
    .forEach(([v, name, stack]) => {
      const b = mk('button', 'pk');
      b.setAttribute('aria-pressed', String(v === CFG.codeFont));
      const n = mk('div', 'n', name);
      n.insertAdjacentHTML('beforeend', TICK);
      const smp = mk('div', 'smp', 'const ok = 0 != O;');
      smp.style.fontFamily = stack;
      b.append(n, smp);
      b.onclick = () => { CFG.codeFont = v; applyLook(); lookSave(); drawSettings(); };
      w.appendChild(b);
    });
  return w;
}
function sempty(c, head, words, bullets) {
  const w = mk('div', 'sempty');
  w.appendChild(mk('div', 'h', head));
  if (words) w.appendChild(mk('div', 'w', words));
  if (bullets && bullets.length) {
    const ul = mk('ul');
    bullets.forEach((b) => ul.appendChild(mk('li', null, b)));
    w.appendChild(ul);
  }
  c.appendChild(w);
  return w;
}
const onoff = (v) => T(v ? 'gui.set.on' : 'gui.set.off');

/* ---- appearance -----------------------------------------------------
   Each front end's own business, so it persists here rather than in the
   shared config -- and it does persist now: these used to toast "saved" and
   forget on reload. Language is the exception, it goes to config.language
   because the TUI and the agent's replies follow it. */
const LOOK_KEY = 'raven.gui.look';
/* Tells the desktop shell what ground colour this window actually shows, so
   its native launch splash can match on the NEXT run -- the page's theme
   lives in this origin's localStorage, which the shell cannot read before
   the page is up. No-op outside the shell. */
function shellTheme() {
  try {
    const t = document.documentElement.dataset.theme
      || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    window.webkit.messageHandlers.raven.postMessage({ type: 'theme', value: t });
  } catch { /* not the shell */ }
}
function applyLook() {
  const d = document.documentElement.dataset;
  if (CFG.theme === 'system') delete d.theme; else d.theme = CFG.theme;
  d.motion = CFG.motion;
  d.font = CFG.codeFont;
  shellTheme();
}
function lookSave() {
  try {
    localStorage.setItem(LOOK_KEY, JSON.stringify(
      { theme: CFG.theme, codeFont: CFG.codeFont, motion: CFG.motion }));
  } catch { /* private mode or quota: this tab only */ }
}
function lookLoad() {
  try {
    const o = JSON.parse(localStorage.getItem(LOOK_KEY) || 'null');
    if (o && typeof o === 'object') {
      if (o.theme) CFG.theme = o.theme;
      if (o.codeFont) CFG.codeFont = o.codeFont;
      if (o.motion) CFG.motion = o.motion;
    }
  } catch { /* fall back to the defaults in CFG */ }
  applyLook();
}

/* ---- notifications ---------------------------------------------------
   Front-end only: the OS notification fires when a turn finishes while the
   window is in the background. Preference persists per front end. */
const NTF_KEY = 'raven.gui.ntf';
const NTF = { on: false };
try {
  const o = JSON.parse(localStorage.getItem(NTF_KEY) || 'null');
  if (o && typeof o === 'object') NTF.on = !!o.on;
} catch { /* default off */ }
function ntfSave() {
  try { localStorage.setItem(NTF_KEY, JSON.stringify(NTF)); } catch { /* private mode */ }
}
function ntfPush(title, body, opts) {
  if (!NTF.on || !('Notification' in window) || Notification.permission !== 'granted') return;
  /* Only when the reader is away: a toast already covers the foreground. */
  if (!(opts && opts.force) && document.hasFocus()) return;
  try { new Notification(title, body ? { body } : undefined); } catch { /* platform quirk */ }
}

const isMac = () => /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const modKey = () => (isMac() ? '⌘' : 'Ctrl +');

function seg(opts, val, fn) {
  const w = mk('div', 'seg');
  opts.forEach(([v, label]) => {
    const b = mk('button', null, label);
    b.setAttribute('aria-pressed', String(v === val));
    b.onclick = () => { fn(v); };
    w.appendChild(b);
  });
  return w;
}
function swi(on, fn, label) {
  const s = mk('button', 'swi');
  s.setAttribute('role', 'switch');
  s.setAttribute('aria-checked', String(on));
  if (label) s.setAttribute('aria-label', label);
  s.onclick = () => { fn(!on); };
  return s;
}
function slider(min, max, step, val, fmt, fn) {
  const w = mk('div'); w.style.cssText = 'display:flex;align-items:center';
  const i = mk('input'); i.type = 'range'; i.min = min; i.max = max; i.step = step; i.value = val;
  const out = mk('span', 'val', fmt(val));
  i.oninput = () => { out.textContent = fmt(Number(i.value)); };
  i.onchange = () => fn(Number(i.value));
  w.append(i, out);
  return w;
}
function textField(val, fn, ph) {
  const i = mk('input'); i.type = 'text'; i.value = val || ''; i.placeholder = ph || '';
  i.style.width = '230px';
  i.onchange = () => fn(i.value);
  return i;
}

/* ---- pages ----------------------------------------------------------
   One renderer per page, both layers. live.js overrides individual entries
   only where the real thing cannot be expressed through V(). */
const SET_PAGE = {

  /* The one module where every control is real, so it is the one module that
     earns visual controls: each option shows the thing it does. */
  look(p) {
    // Language first: it is the one switch that relabels every other row here,
    // in the TUI, and in what the agent writes back.
    const l = scard(p, T('gui.set.language'));
    l.appendChild(pick([['zh', T('gui.set.language_zh')], ['en', T('gui.set.language_en')]], LANG, (v) => {
      if (applyLang) { applyLang(v, { persist: true }); return; }
      LANG = v; applyI18n(); drawList(); drawSettings(); drawPerm(); drawCtx(); drawFoot();
    }));

    const t = scard(p, T('gui.set.theme'));
    t.appendChild(themePick());

    const f = scard(p, T('gui.set.codefont'));
    f.appendChild(fontPick());

    const m = scard(p);
    crow(m, T('gui.set.motion'), swi(CFG.motion === 'off', (v) => {
      CFG.motion = v ? 'off' : 'on'; applyLook(); lookSave(); drawSettings();
    }, T('gui.set.motion')), T('gui.set.motion_w'));
  },

  notify(p) {
    const c = scard(p, T('gui.set.ntf.all'));
    const canNtf = 'Notification' in window;
    crow(c, T('gui.set.ntf.done'), swi(NTF.on, async (v) => {
      if (v && canNtf && Notification.permission !== 'granted') {
        const r = await Notification.requestPermission();
        if (r !== 'granted') { NTF.on = false; ntfSave(); drawSettings(); toast(T('gui.set.ntf.denied')); return; }
      }
      NTF.on = v && canNtf;
      ntfSave(); drawSettings();
      if (v && !canNtf) toast(T('gui.set.ntf.denied'));
    }, T('gui.set.ntf.all')));
    if (NTF.on) {
      const b = mk('button', 'mini ghost', T('gui.set.ntf.test'));
      b.onclick = () => ntfPush(T('gui.set.ntf.test_body'), '', { force: true });
      crow(c, T('gui.set.ntf.test'), b);
    }
  },

  usage(p) {
    /* Every open re-reads the counters -- what is on screen is a running
       total, and a cached one is wrong the moment the next call lands.
       The numbers already held are drawn immediately, so the refresh is
       silent rather than a skeleton flash. */
    if (usageLoad) usageLoad();
    if (!USAGE) {
      const c = scard(p);
      c.appendChild(mk('div', 'empty-note', T(usageLoad ? 'gui.set.usg.loading' : 'gui.set.nodata')));
      return;
    }
    const fmtTok = (n) => (n >= 1e6 ? (n / 1e6).toFixed(1) + 'M'
      : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n));
    const u = USAGE;
    const c = scard(p, `${T('gui.set.usg.llm')} · ${T('gui.set.usg.window', { d: u.days })}`);
    c.appendChild(statTiles([
      [String(u.llm.total.calls), T('gui.set.usg.calls')],
      [fmtTok(u.llm.total.input_tokens), T('gui.set.usg.in')],
      [fmtTok(u.llm.total.output_tokens), T('gui.set.usg.out')],
      ['$' + u.llm.total.cost_usd.toFixed(2), T('gui.set.usg.cost')]]));
    if (u.llm.models.length) {
      c.appendChild(kvList(u.llm.models.slice(0, 12).map((m) => [m.model,
        `${m.calls} × · ${fmtTok(m.input_tokens + m.output_tokens)} tok · $${m.cost_usd.toFixed(2)}`])));
    }
    const t = scard(p, `${T('gui.set.usg.tools')} · ${T('gui.set.usg.window', { d: u.days })}`);
    if (!u.tools.counts.length) {
      t.appendChild(mk('div', 'empty-note', T('gui.set.nodata')));
    } else {
      t.appendChild(statTiles([[String(u.tools.total), T('gui.set.usg.calls')]]));
      t.appendChild(kvList(u.tools.counts.slice(0, 14).map((x) => [x.name, `${x.count} ×`])));
    }
  },

  keys(p) {
    const m = modKey();
    const groups = [
      ['gui.set.kbd.grp_chat', [['gui.set.kbd.send', 'Enter'],
        ['gui.set.kbd.newline', 'Shift + Enter'], ['gui.set.kbd.stop', 'Esc']]],
      ['gui.set.kbd.grp_nav', [['gui.set.kbd.newtask', `${m} N`],
        ['gui.set.kbd.find', `${m} F`], ['gui.set.kbd.rail', `${m} \\`]]],
      ['gui.set.kbd.grp_panel', [['gui.set.kbd.ws', '1 – 4'], ['gui.set.kbd.close', 'Esc']]]
    ];
    const c = scard(p, null, null);
    const w = mk('div', 'kbdg');
    groups.forEach(([gk, rows]) => {
      const g = mk('div', 'g');
      g.appendChild(mk('div', 'gt', T(gk)));
      rows.forEach(([k, key]) => {
        const r = mk('div', 'r');
        r.append(mk('span', null, T(k)), mk('kbd', null, key));
        g.appendChild(r);
      });
      w.appendChild(g);
    });
    c.appendChild(w);
  },

  about(p) {
    /* Identity, then the two things a person comes here for: the log to hand
       over and the doors out. One card -- three sparse cards read as filler. */
    const c = scard(p);
    const id = mk('div', 'abtid');
    const nm = mk('div', 'nm', 'Raven');
    nm.appendChild(mk('span', 'ver', APP_VERSION || '--'));
    id.appendChild(nm);
    const up = mk('button', 'mini ghost', T('gui.set.check_update'));
    up.onclick = () => checkUpdate(up);
    id.appendChild(up);
    c.appendChild(id);

    const d = scard(p, T('gui.set.diag'));
    const lr = mk('div', 'crow');
    lr.appendChild(mk('div', 'k', T('gui.set.logs')));
    const lc = mk('div', 'c abtlog');
    lc.append(mk('code', null, '~/.raven/logs/tui.log'),
      cpBtn(T('gui.set.abt.copy_path'), '~/.raven/logs/tui.log'));
    lr.appendChild(lc);
    d.appendChild(lr);
    const links = mk('div', 'srow');
    [['gui.set.abt.docs', 'https://raven.evermind.ai'],
     ['gui.set.abt.repo', 'https://github.com/EverMind-AI/Raven']].forEach(([key, u]) => {
      const b = mk('button', 'mini ghost', T(key));
      b.onclick = () => wsOpenUrl(u);
      links.appendChild(b);
    });
    d.appendChild(links);
  },

  model(p) {
    const card = scard(p, T('gui.set.mdl.providers'));
    const set = card.appendChild(mk('div', 'fset'));
    PROVIDERS.forEach((pv) => {
      const c = mk('div', 'pcard');
      const nm = mk('div', 'nm');
      nm.append(mk('span', 'led' + (pv.on ? '' : ' warn')), mk('span', null, pv.name));
      c.append(nm, mk('div', 'mo', pv.models.join(' · ')));
      const ctl = mk('div', 'ctl');
      if (pv.on) {
        const d = mk('button', 'mini ghost', T('gui.model.disconnect'));
        d.onclick = () => confirmAsk(T('gui.model.disconnect'),
          T('gui.model.disconnect_body', { name: pv.name }), T('gui.model.disconnect'), () => {
            pv.on = false; pv.key = '';
            if (pv.models.includes(model)) {
              const alt = PROVIDERS.find((x) => x.on);
              model = alt ? alt.models[0] : '—';
              $('#modelName').textContent = model;
            }
            drawSettings();
          });
        ctl.appendChild(d);
      } else {
        ctl.appendChild(roVal(T('gui.model.not_connected'), true));
      }
      c.appendChild(ctl);
      const kr = mk('div', 'keyrow');
      const i = mk('input'); i.type = 'password'; i.value = pv.key;
      i.placeholder = `${pv.name} API Key`;
      i.setAttribute('aria-label', `${pv.name} API Key`);
      const b = mk('button', 'mini', T(pv.on ? 'gui.model.update' : 'gui.model.connect'));
      b.onclick = () => {
        if (!i.value.trim()) { i.focus(); return; }
        pv.key = i.value; pv.on = true; drawSettings();
      };
      kr.append(i, b);
      c.appendChild(kr);
      set.appendChild(c);
    });
    modelTuning(p);
  },

  /* Three modes with a sentence each: a segmented control cannot say what
     "read-only" would actually stop the agent from doing, and that sentence is
     the whole reason a person opens this section. */
  perm(p) {
    /* Per-session permission lives next to the composer (完全访问); this page
       owns only the standing guard rails.

       Read-only on purpose, not for lack of wiring: these two ARE the
       containment controls, and settings.set is reachable from any RPC client
       with no confirmation step -- one call would turn a sandboxed agent into
       an unsandboxed one. The server refuses them (see _SETTINGS_SIMPLE_KEYS),
       so a switch here was a control that toasted an error on every flip.
       Editing the config file is the friction, and it is the point. */
    const g = scard(p, T('gui.set.prm.guard'));
    const sb = String(V('tools.sandbox.backend', 'none'));
    const sbName = sb === 'none' ? T('gui.set.prm.sb_none') : sb === 'auto' ? T('gui.set.prm.sb_auto') : sb;
    g.appendChild(kvList([
      [T('gui.set.prm.workspace'), onoff(V('tools.restrictToWorkspace', false) === true)],
      [T('gui.set.prm.sandbox'), sbName, sb === 'none' ? 'unset' : 'ok']]));
    const why = mk('div', 'srmk');
    why.append(mk('span', null, T('gui.set.prm.file_only')),
      mk('code', null, CONFIG_PATH), cpBtn(T('gui.set.abt.copy_path'), CONFIG_PATH));
    g.appendChild(why);
  },

  /* The built-in inventory, grouped by what it touches. No add and no remove:
     what can grow lives in 插件; what is listed here only turns on and off. */
  toolset(p) {
    TOOL_GROUPS.forEach((g) => {
      const items = TOOLS.filter((t) => t.group === g.id);
      if (!items.length) return;
      const on = items.filter((t) => t.on).length;
      const c = scard(p, T(g.label), T('gui.caps.tool_on', { on, all: items.length }));
      const set = mk('div', 'fset');
      items.forEach((t) => {
        set.appendChild(toolLine(t));
        if (TOOL_CRED[t.id] && toolKeyEdit === t.id) set.appendChild(toolCredRow(t, TOOL_CRED[t.id]));
      });
      c.appendChild(set);
    });
  },

  memory(p) {
    const c = scard(p, T('gui.set.memory'));
    crow(c, T('gui.set.mem.topk'), wNum('memory.memoryTopK', Number(V('memory.memoryTopK', 5)), 1, 50));
    crow(c, T('gui.set.mem.learn'),
      wSwi('agents.defaults.enablePersonalization',
        V('agents.defaults.enablePersonalization', false) === true, T('gui.set.mem.learn')));

    /* The backend is not a choice: long-term memory runs on EverOS. What a
       person configures is EverOS itself -- the model behind each of its
       roles. Extraction + embedding are required; rerank/multimodal fold. */
    const m = scard(p, T('gui.set.mem.models'));
    const secs = (EVEROS && EVEROS.sections) || {};
    const set = mk('div', 'fset');
    [['llm', 'gui.set.mem.role_llm', true], ['embedding', 'gui.set.mem.role_embedding', true],
      ['rerank', 'gui.set.mem.role_rerank', false], ['multimodal', 'gui.set.mem.role_multimodal', false]]
      .forEach(([sec, key, required]) => {
        const cur = secs[sec] || {};
        const on = !!(cur.model && cur.api_key_set);
        const row = mk('div', 'mrole');
        const hd = mk('div', 'hd');
        const chip = mk('span', 'kchip' + (on ? '' : ' off'));
        chip.append(mk('span', 'led'), mk('span', null, T(key)));
        chip.title = on ? T('gui.set.tls.key_set') : T('gui.set.tls.key_unset');
        hd.appendChild(chip);
        hd.appendChild(mk('span', 'mo' + (cur.model ? '' : ' dim'), cur.model || T('gui.set.unset')));
        const ed = mk('button', 'mini ghost',
          T(memEdit === sec ? 'gui.set.mem.fold' : (on ? 'gui.model.update' : 'gui.set.mem.setup')));
        ed.onclick = () => { memEdit = memEdit === sec ? null : sec; drawSettings(); };
        hd.appendChild(ed);
        row.appendChild(hd);
        if (memEdit === sec) {
          const f = mk('div', 'ff');
          const im = mk('input'); im.type = 'text'; im.value = cur.model || '';
          im.placeholder = T('gui.set.mem.model_ph');
          const iu = mk('input'); iu.type = 'text'; iu.value = cur.base_url || '';
          iu.placeholder = 'https://api.example.com/v1';
          const ik = mk('input'); ik.type = 'password'; ik.autocomplete = 'off';
          ik.placeholder = cur.api_key_set ? T('gui.set.tls.key_set') : 'API Key';
          const acts = mk('div', 'mfacts');
          const sv = mk('button', 'mini', T('gui.set.mem.save'));
          sv.onclick = () => {
            if (!everosWrite) { nlSay(sv); return; }
            const fields = {};
            if (im.value.trim()) fields.model = im.value.trim();
            if (iu.value.trim()) fields.base_url = iu.value.trim();
            if (ik.value.trim()) fields.api_key = ik.value.trim();
            if (!Object.keys(fields).length) { im.focus(); return; }
            everosWrite(sec, fields, sv);
          };
          acts.appendChild(sv);
          if (!required && (cur.model || cur.api_key_set)) {
            const off = mk('button', 'mini ghost', T('gui.set.mem.disable'));
            off.onclick = () => (everosWrite ? everosWrite(sec, null, off) : nlSay(off));
            acts.appendChild(off);
          }
          f.append(im, iu, ik, acts);
          row.appendChild(f);
        }
        set.appendChild(row);
      });
    m.appendChild(set);
  },

  /* An entry-point page on purpose: proactivity is a module of its own and
     turning it on is not one switch. The page says what it will do. */
  proact(p) {
    const c = scard(p);
    sempty(c, T('gui.set.pro.hero'));

    const ch = scard(p, T('gui.set.pro.channels'));
    const go = mk('button', 'mini ghost', T('gui.set.chn.manage'));
    go.onclick = () => { closeSet(); openConn(); };
    ch.appendChild(go);
  },

  exec(p) {
    const c = scard(p, T('gui.set.exe.card'));
    /* The proxy is display-only for the same reason the sandbox backend is:
       it routes every WebSearch and WebFetch, API keys and all, so the server
       refuses to take it over settings.set. */
    c.appendChild(kvList([
      [T('gui.set.cwd'), String(V('agents.defaults.workspace', '~/.raven/workspace'))],
      [T('gui.set.exe.path'), String(V('tools.exec.pathAppend', '')) || T('gui.set.unset'),
        V('tools.exec.pathAppend', '') ? '' : 'unset'],
      [T('gui.set.exe.proxy'), String(Vnull('tools.web.proxy', '') || '') || T('gui.set.unset'),
        Vnull('tools.web.proxy', '') ? '' : 'unset']]));
    crow(c, T('gui.set.prm.timeout'),
      wNum('tools.exec.timeout', Number(V('tools.exec.timeout', 60)), 5, 3600));
    const why = mk('div', 'srmk');
    why.append(mk('span', null, T('gui.set.prm.file_only')),
      mk('code', null, CONFIG_PATH), cpBtn(T('gui.set.abt.copy_path'), CONFIG_PATH));
    c.appendChild(why);
  },

  channel(p) {
    const fwd = V('cron.forwardChannels', []) || [];
    const c = scard(p, T('gui.set.chn.card'));
    crow(c, T('gui.set.chn.progress'),
      wSwi('channels.sendProgress', V('channels.sendProgress', true) === true, T('gui.set.chn.progress')));
    crow(c, T('gui.set.chn.hints'),
      wSwi('channels.sendToolHints', V('channels.sendToolHints', false) === true, T('gui.set.chn.hints')));
    crow(c, T('gui.set.chn.cron_to'),
      wText('cron.forwardChannels', fwd.join(', '), T('gui.set.chn.cron_ph'),
        (v) => v.split(',').map((x) => x.trim()).filter(Boolean)));
    crow(c, T('gui.set.chn.tz'),
      wText('cron.defaultTimezone', String(V('cron.defaultTimezone', 'Asia/Shanghai')), 'Asia/Shanghai',
        (v) => (v.trim() ? v.trim() : undefined)));

    const e = scard(p, T('gui.set.chn.entry'));
    const go = mk('button', 'mini ghost', T('gui.set.chn.manage'));
    go.onclick = () => { closeSet(); openConn(); };
    e.appendChild(go);
  },

  data(p) {
    const c = scard(p, T('gui.set.dat.where'));
    /* No disk-usage row: nothing measures it, and a permanent 无数据 reads as
       a broken counter rather than an absent feature. */
    c.appendChild(kvList([
      [T('gui.set.dat.config'), CONFIG_PATH],
      [T('gui.set.store'), String(V('agents.defaults.workspace', '~/.raven/workspace'))]]));
    const acts = mk('div', 'srow');
    acts.append(cpBtn(T('gui.set.abt.copy_path'), CONFIG_PATH));
    c.appendChild(acts);

    /* The destructive action gets its own card and its own colour: it must not
       sit one row below "copy path" as if it were the same kind of thing. */
    const d = scard(p, T('gui.set.danger'));
    const dl = mk('button', 'mini ghost danger', T('gui.set.delete_all'));
    dl.onclick = () => confirmAsk(T('gui.set.delete_all'),
      T('gui.set.delete_all_body', { n: SESS.length }), T('gui.set.delete_all_yes'),
      () => deleteAllSessions());
    d.appendChild(dl);
  }
};

/* Sampling and routing sit with the model rather than in a separate
   "behaviour" page: every one of them is "how this model answers". */
function modelTuning(p) {
  const c = scard(p, T('gui.set.mdl.tuning'));
  /* Reasoning effort is the one sampling knob a person actually reaches for;
     temperature/thinking-budget sliders were dead weight (no runtime consumer
     for the budget, a config-corrupting writer for both) and are gone. */
  c.appendChild(wPick('agents.defaults.reasoningEffort',
    [['minimal', T('gui.set.mdl.eff_min')], ['low', T('gui.set.mdl.eff_low')],
      ['medium', T('gui.set.mdl.eff_med')], ['high', T('gui.set.mdl.eff_high')]],
    String(V('agents.defaults.reasoningEffort', 'medium'))));

  /* The three ceilings are read-only facts, and they are what a reader asks for
     only after something hit one -- so they stay behind a fold. */
  const fold = mk('button', 'foldrow', T(mdlAdv ? 'gui.set.mdl.adv_hide' : 'gui.set.mdl.adv'));
  fold.setAttribute('aria-expanded', String(mdlAdv));
  fold.onclick = () => { mdlAdv = !mdlAdv; drawSettings(); };
  p.appendChild(fold);
  if (!mdlAdv) return;
  const a = scard(p, T('gui.set.mdl.limits'));
  a.appendChild(kvList([
    [T('gui.set.mdl.maxtok'), String(V('agents.defaults.maxTokens', 8192))],
    [T('gui.set.mdl.ctx'), String(V('agents.defaults.contextWindowTokens', 65536))],
    [T('gui.set.mdl.iter'), String(V('agents.defaults.maxToolIterations', 40))]]));
}

/* One glyph per section: with 14 of them in a 208px rail, a shape is what the
   eye returns to, and the words are what it reads once it is there. */
const SET_ICO = {
  account: '<circle cx="12" cy="8.5" r="3.6"/><path d="M5 20c1.2-3.5 3.8-5.2 7-5.2s5.8 1.7 7 5.2"/>',
  usage: '<path d="M4 19h16"/><path d="M7 19v-6M12 19V6M17 19v-9"/>',
  look: '<circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 0 0 16Z" fill="currentColor" stroke="none"/>',
  notify: '<path d="M12 4a5 5 0 0 0-5 5v4l-1.5 3h13L17 13V9a5 5 0 0 0-5-5Z"/><path d="M10 20h4"/>',
  keys: '<rect x="3" y="6.5" width="18" height="11" rx="2.2"/><path d="M7 10h.01M11 10h.01M15 10h.01M8 14h8"/>',
  about: '<circle cx="12" cy="12" r="8"/><path d="M12 11v5M12 8h.01"/>',
  model: '<path d="M12 3.5 20 8v8l-8 4.5L4 16V8l8-4.5Z"/><path d="M12 12v8.5M12 12 4 8M12 12l8-4"/>',
  perm: '<path d="M12 3.5 19 6v5.5c0 4-2.9 7.4-7 9-4.1-1.6-7-5-7-9V6l7-2.5Z"/>',
  memory: '<rect x="4" y="4.5" width="16" height="15" rx="2.4"/><path d="M8 9h8M8 12.5h8M8 16h5"/>',
  proact: '<path d="M13 3 5.5 13.5H11l-1 7.5 8-11H12l1-7Z"/>',
  exec: '<rect x="3.5" y="5" width="17" height="14" rx="2.4"/><path d="m7.5 10 2.5 2-2.5 2M13 14h4"/>',
  toolset: '<path d="M14.5 4.5a4.2 4.2 0 0 0 5.5 5.6L14 16.2l-4-4 4.5-7.7Z"/><path d="m9 13-4.5 4.5a1.8 1.8 0 0 0 2.5 2.5L11.5 16"/>',
  tools: '<circle cx="8" cy="12" r="3.5"/><path d="M11.5 12H20M16.5 12v3M20 12v2.5"/>',
  channel: '<path d="M4 7.5h16v9H4z"/><path d="m4 8 8 5 8-5"/>',
  data: '<ellipse cx="12" cy="6.5" rx="7" ry="2.8"/><path d="M5 6.5v11c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8v-11"/><path d="M5 12c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8"/>'
};

function drawSettings() {
  const nav = $('#snavList'); nav.innerHTML = '';
  SET_GROUPS.forEach((g) => {
    nav.appendChild(mk('div', 'grp', T(g.key)));
    g.pages.forEach(([id, key]) => {
      const b = mk('button', 'sitem');
      b.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${SET_ICO[id] || SET_ICO.about}</svg>`;
      b.appendChild(mk('span', null, T(key)));
      b.setAttribute('aria-current', String(id === sTab));
      b.onclick = () => { sTab = id; drawSettings(); };
      nav.appendChild(b);
    });
  });
  /* In the narrow chip-strip mode the active section can sit past the fold;
     bring it back into view whenever the dialog redraws. */
  const curBtn = nav.querySelector('[aria-current="true"]');
  if (curBtn && curBtn.scrollIntoView) curBtn.scrollIntoView({ block: 'nearest', inline: 'nearest' });

  $('#setTitle').textContent = T(SET_TITLE[sTab] || 'gui.nav.set');
  const sub = $('#setSub');
  sub.textContent = ''; sub.hidden = true;

  const host = $('#spanels'); host.innerHTML = '';
  const p = mk('div', 'panel'); p.dataset.on = 'true';
  host.appendChild(p);
  (SET_PAGE[sTab] || SET_PAGE.usage)(p);
}

// Everything runs on this machine; the chip is a label, not a switch.
function setRuntime() {
  $('#envName').textContent = '本机';
  $('#envChip').querySelector('.led').className = 'led';
}


