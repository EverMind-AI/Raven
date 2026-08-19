/* -- workspace: real file browsing + live file view ------------------ */

function relToWorkspace(p) {
  const s = String(p || '');
  const m = s.match(/(?:^|\/)(?:\.raven\/)?workspace\/(.+)$/);
  if (m) return m[1];
  if (s.startsWith('/') || s.startsWith('~')) return null;
  return s.replace(/^\.\//, '');
}

/* "Looks like a path" is not enough to make one clickable: most repo-shaped
   strings in an answer are not files, and a link that opens onto an error is
   worse than plain text. Two cases are provable without a round trip: the text
   names the workspace, or Raven touched that file this session -- and since the
   viewer is no longer confined to the workspace, the second case now counts
   wherever the file lives. Everything else stays plain text. */
wsPathOf = (s) => {
  const t = String(s).trim().replace(/:\d+(?::\d+)?$/, '');
  if (!t || /\s/.test(t)) return null;
  if (/(?:^|\/)(?:\.raven\/)?workspace\/./.test(t)) return relToWorkspace(t) || t;
  const hit = WS.changes.find((c) => c.key === t || wsShortPath(c.key) === t);
  return hit ? hit.key : null;
};
pathOpen = (rel) => showFile(rel);

/* An explicit markdown link is the author handing something over, so the live
   resolver is broader than the bare-span one above: absolute paths, ~ paths
   and workspace-relative shapes all count, extension decides file-or-folder.
   file:// is stripped rather than rejected -- models write it out of habit. */
linkTargetOf = (u) => {
  let t = String(u).trim().replace(/^file:\/\//, '');
  if (!t || /\s|[<>"']/.test(t)) return null;
  const dirMark = /\/$/.test(t);
  t = t.replace(/\/+$/, '');
  if (!/\//.test(t)) return null;
  /* Segments take any non-separator character: deliverables are routinely
     named in the user's language (HANDOFF-开发交接.md), and \w-only segments
     silently dropped every one of those back to plain text. */
  const shaped = /^(?:\/|~\/)/.test(t) || /^[^\s/\\]+(?:\/[^\s/\\]+)+$/.test(t);
  if (!shaped) return null;
  return { p: t, dir: dirMark || !/\.\w{1,8}$/.test(t) };
};

/* Open a folder where folders live: the file tab's tree, unfolded down to it.
   The root listing is fetched first because tree entries are root-relative and
   the root is only learned from fs.list's answer. */
dirOpen = (p) => {
  wsPicked = true;
  if (!wsOpen) setWs(true, 'file'); else { wsTab = 'file'; drawWs(); }
  ftFetch('').then(() => {
    const rel = relToRoot(p);
    ftReveal(rel != null ? rel : String(p).replace(/^\/+/, ''), true);
  }).catch(() => {});
};

/* The viewer reads over HTTP rather than through fs.read: an image or a PDF
   needs a URL a tag can point at, and the endpoint resolves exactly what the
   agent may read, so a file outside the workspace opens too. */
const fileURL = (p) => '/file?path=' + encodeURIComponent(String(p));

const TEXT_EXT = new Set(['c', 'cfg', 'conf', 'cpp', 'css', 'diff', 'env', 'go', 'h', 'ini', 'java',
  'js', 'json', 'jsonl', 'jsx', 'kt', 'log', 'lua', 'patch', 'php', 'pl', 'py', 'pyi', 'rb', 'rs',
  'sh', 'sql', 'swift', 'toml', 'ts', 'tsx', 'txt', 'vue', 'yaml', 'yml', 'zsh']);
const IMG_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'avif']);

/* What to render, decided from the name alone: the endpoint's content type says
   the same thing, but the view has to be laid out before the bytes arrive. */
function fileKind(p) {
  const ext = (String(p).split('.').pop() || '').toLowerCase();
  if (ext === 'md' || ext === 'mdx' || ext === 'markdown') return 'md';
  if (IMG_EXT.has(ext)) return 'img';
  if (ext === 'svg') return 'svg';
  if (ext === 'pdf') return 'pdf';
  if (ext === 'html' || ext === 'htm') return 'html';
  if (ext === 'csv' || ext === 'tsv') return 'csv';
  if (ext === 'json') return 'json';
  if (ext === 'diff' || ext === 'patch') return 'diff';
  if (TEXT_EXT.has(ext)) return 'code';
  return 'bin';
}
const RENDERED = { md: 1, img: 1, svg: 1, pdf: 1, html: 1, csv: 1, json: 1 };

async function showFile(p) {
  const kind = fileKind(p);
  WS.file = { path: String(p), kind, raw: false, text: null, err: null, size: null, loading: false };
  /* Asking for one named file IS picking the file view -- without this the
     panel opens on its launcher and the file that was just read is nowhere. */
  wsPicked = true;
  if (!wsOpen) setWs(true, 'file'); else { wsTab = 'file'; drawWs(); }
}

/* Only the kinds the page itself parses are fetched as text; an image or a PDF
   is handed to the tag that knows how to draw it. */
async function loadFileText(f) {
  const epoch = wsEpoch;
  f.loading = true;
  try {
    const r = await fetch(fileURL(f.path), { credentials: 'same-origin' });
    if (!r.ok) throw new Error(r.status === 403 ? T('gui.ws.file_denied')
      : r.status === 404 ? T('gui.ws.file_gone')
        : r.status === 413 ? T('gui.ws.file_big') : `HTTP ${r.status}`);
    f.text = await r.text();
  } catch (e) {
    f.err = e.message || String(e);
  } finally {
    f.loading = false;
    if (WS.file === f && !wsStale(epoch)) drawWs();
  }
}

function csvTable(text, tab) {
  const rows = text.replace(/\r/g, '').split('\n').filter((l) => l.length).slice(0, 500)
    .map((l) => l.split(tab ? '\t' : ','));
  const t = mk('table', 'csvt');
  rows.forEach((cells, i) => {
    const tr = mk('tr');
    cells.forEach((c) => tr.appendChild(mk(i === 0 ? 'th' : 'td', null, c)));
    t.appendChild(tr);
  });
  const w = mk('div', 'csvw');
  w.appendChild(t);
  return w;
}

/* The browser's PDF viewer is script-driven and draws nothing in a frame with
   scripts denied, so a PDF gets allow-scripts. The origin stays opaque either
   way -- allow-same-origin is never granted -- so nothing in the frame can
   reach this page's cookie or its RPC socket. HTML stays script-free. */
function frameFor(p, kind) {
  const f = mk('iframe');
  f.setAttribute('sandbox', kind === 'pdf' ? 'allow-scripts' : '');
  f.setAttribute('referrerpolicy', 'no-referrer');
  f.src = fileURL(p);
  return f;
}

function fileBody(f) {
  const v = mk('div', 'fview');
  if (f.err) { v.appendChild(mk('div', 'verr', f.err)); return v; }
  const asSource = f.raw || !RENDERED[f.kind];
  if (f.kind === 'img' || (f.kind === 'svg' && !asSource)) {
    const shot = mk('div', 'shot');
    const img = mk('img');
    img.src = fileURL(f.path);
    img.alt = f.path;
    img.onerror = () => { shot.innerHTML = ''; shot.appendChild(mk('div', 'verr', T('gui.ws.file_gone'))); };
    shot.appendChild(img);
    v.appendChild(shot);
    return v;
  }
  if ((f.kind === 'pdf' || f.kind === 'html') && !asSource) {
    v.appendChild(frameFor(f.path, f.kind));
    return v;
  }
  if (f.kind === 'bin') {
    const n = mk('div', 'binote');
    n.append(mk('div', 'h', T('gui.ws.file_binary')), mk('div', 'w', f.path));
    const b = mk('button', 'mini ghost', T('gui.ws.copy_path_do'));
    b.onclick = () => navigator.clipboard && navigator.clipboard.writeText(f.path);
    n.appendChild(b);
    v.appendChild(n);
    return v;
  }
  if (f.text == null) {
    if (!f.loading) loadFileText(f);
    v.appendChild(mk('div', 'vspin', T('gui.ws.file_loading')));
    return v;
  }
  if (f.kind === 'md' && !asSource) {
    const pr = mk('div', 'prose');
    pr.innerHTML = md(f.text);
    v.appendChild(pr);
    return v;
  }
  if (f.kind === 'csv' && !asSource) {
    v.appendChild(csvTable(f.text, /\.tsv$/i.test(f.path)));
    return v;
  }
  if (f.kind === 'json' && !asSource) {
    const tree = jsonTree(f.text);
    /* A file that does not parse is still a file: fall through to the plain
       numbered lines rather than showing an error for something readable. */
    if (tree) { v.appendChild(tree); return v; }
  }
  const code = mk('div', 'code');
  f.text.split('\n').forEach((line, i) => {
    const row = mk('div', 'ln' + (f.kind === 'diff' ? diffLineCls(line) : ''));
    row.append(mk('i', null, String(i + 1)), mk('span', null, line || ' '));
    code.appendChild(row);
  });
  v.appendChild(code);
  return v;
}

/* A patch is one of the few formats whose lines carry their meaning in the
   first character; without colour it reads as noise with plus signs. */
function diffLineCls(line) {
  if (/^(\+\+\+|---)/.test(line)) return ' dmeta';
  if (line[0] === '+') return ' dadd';
  if (line[0] === '-') return ' ddel';
  if (line.startsWith('@@')) return ' dhunk';
  if (/^(diff |index |new file|deleted file|similarity |rename |Binary )/.test(line)) return ' dmeta';
  return '';
}

/* ── json: a foldable tree ────────────────────────────────────────────
   Native <details> does the folding, so there is no open-state to manage and
   the keyboard works for free. A node budget keeps a machine-dumped megabyte
   from freezing the tab: past it, the rest of a container is summarised and
   the source toggle is the way to read that far. */
const JSON_NODE_BUDGET = 4000;

function jsonTree(text) {
  if (text.length > 2 * 1024 * 1024) return null;
  let val;
  try { val = JSON.parse(text); } catch { return null; }
  const box = mk('div', 'jsonv');
  const state = { left: JSON_NODE_BUDGET };
  box.appendChild(jsonNode(val, null, state, 0));
  return box;
}

const jsonLeaf = (v) => {
  const s = mk('span', 'jv ' + (v === null ? 'jnull' : typeof v === 'string' ? 'jstr'
    : typeof v === 'number' ? 'jnum' : 'jbool'));
  s.textContent = v === null ? 'null' : typeof v === 'string' ? JSON.stringify(v) : String(v);
  return s;
};

function jsonNode(v, key, state, depth) {
  state.left -= 1;
  const keySpan = () => {
    const k = mk('span', 'jk');
    k.textContent = typeof key === 'number' ? String(key) : JSON.stringify(key);
    return k;
  };
  if (v === null || typeof v !== 'object') {
    const row = mk('div', 'jrow');
    if (key !== null) { row.appendChild(keySpan()); row.appendChild(mk('span', 'jc', ':')); }
    row.appendChild(jsonLeaf(v));
    return row;
  }
  const isArr = Array.isArray(v);
  const entries = isArr ? v.map((x, i) => [i, x]) : Object.entries(v);
  const d = document.createElement('details');
  d.className = 'jnode';
  /* The first two levels open by default: that is the shape of the file. Below
     that the reader opens what they are looking for. */
  if (depth < 2) d.open = true;
  const sum = document.createElement('summary');
  if (key !== null) { sum.appendChild(keySpan()); sum.appendChild(mk('span', 'jc', ':')); }
  sum.appendChild(mk('span', 'jb', isArr ? '[' : '{'));
  sum.appendChild(mk('span', 'jn', T('gui.ws.json_items', { n: String(entries.length) })));
  sum.appendChild(mk('span', 'jb', isArr ? ']' : '}'));
  d.appendChild(sum);
  const kids = mk('div', 'jkids');
  for (const [k, child] of entries) {
    if (state.left <= 0) {
      kids.appendChild(mk('div', 'jrow jmore', T('gui.ws.json_capped')));
      break;
    }
    kids.appendChild(jsonNode(child, k, state, depth + 1));
  }
  if (!entries.length) kids.appendChild(mk('div', 'jrow jmore', isArr ? '[]' : '{}'));
  d.appendChild(kids);
  return d;
}

/* ── file tree ────────────────────────────────────────────────────────
   One tree over the whole workspace instead of a one-folder-at-a-time drill:
   folders open in place, so where a file sits stays visible while you read it.
   Listings are fetched per folder on first open and kept -- fs.list is a round
   trip, and a tree that re-fetched on every keystroke would flicker. */
const FT = { open: new Set(['']), kids: new Map(), q: '', w: 208, hide: false,
  list: null, run: 0, crawling: false, capped: false, root: '' };
const FTW_KEY = 'raven.gui.ftw';
try { FT.w = Math.max(150, Math.min(460, parseFloat(localStorage.getItem(FTW_KEY)) || FT.w)); } catch {}

/* A width and a collapsed tree are how this reader likes to work, not part of
   the workspace, so they survive a session switch. */
function ftReset() {
  FT.open = new Set(['']); FT.kids.clear();
  FT.q = ''; FT.run += 1; FT.crawling = false; FT.capped = false;
  FT.root = '';
}

/* Tree entries are root-relative; the viewer and the reveal RPC take real
   paths, so the join happens at the moment a row leaves the tree. */
const ftAbs = (full) => (FT.root ? `${FT.root}/${full}` : full);
const relToRoot = (p) => {
  const s = String(p || '');
  return FT.root && s.startsWith(FT.root + '/') ? s.slice(FT.root.length + 1) : null;
};

/* A different session is a different workspace state: keep no listing that was
   read before the switch. */
const wsResetBase = wsReset;
wsReset = function () { wsResetBase(); ftReset(); };

/* One promise per directory, shared by the tree and the search crawler, so a
   folder the crawler already asked for is never fetched again when it is
   opened by hand -- and vice versa. */
const ftPending = new Map();
function ftFetch(dir) {
  if (FT.kids.has(dir)) return Promise.resolve(FT.kids.get(dir));
  if (ftPending.has(dir)) return ftPending.get(dir);
  const p = rpc.call('fs.list', { path: dir, session: cur || '' })
    .then((r) => {
      /* The root rides on every answer: it is the session's working directory,
         which the server resolves and this side has no way to guess. */
      if (r.root) FT.root = r.root;
      /* Directories first, each group alphabetical -- the order every file
         manager has trained people to expect. */
      const kids = [...r.entries].sort((a, b) =>
        (b.dir ? 1 : 0) - (a.dir ? 1 : 0) || a.name.localeCompare(b.name));
      FT.kids.set(dir, kids);
      return kids;
    })
    .catch((e) => {
      const bad = { err: e.message || String(e) };
      FT.kids.set(dir, bad);
      return bad;
    })
    .finally(() => ftPending.delete(dir));
  ftPending.set(dir, p);
  return p;
}

function ftLoad(dir) {
  if (FT.kids.has(dir)) return;
  ftFetch(dir).then(() => ftDraw());
}

/* Redraws only the row list: the search field above it must survive every
   redraw, or a listing that lands mid-word steals the focus and the caret. */
function ftDraw() {
  const list = FT.list;
  if (!list || !list.isConnected) {
    if (wsTab === 'file' && wsOpen) drawWs();
    return;
  }
  const y = list.scrollTop;
  list.innerHTML = '';
  if (FT.q) ftResults(list); else ftRows(list, '', 0);
  list.scrollTop = y;
}

const ftJoin = (dir, name) => (dir ? `${dir}/${name}` : name);
const ftKind = (name) => {
  const k = fileKind(name);
  if (k === 'md') return 'md';
  if (k === 'img' || k === 'svg' || k === 'pdf') return 'img';
  if (/\.(json|ya?ml|toml|ini|cfg|conf|lock|env)$/i.test(name)) return 'cfg';
  return k === 'code' || k === 'html' || k === 'csv' ? 'code' : 'doc';
};

/* ── workspace search ─────────────────────────────────────────────────
   A query walks the whole workspace, not just the folders that happen to be
   open: fs.list is the only primitive the host offers, so the client crawls --
   breadth-first, a few folders at a time, against a budget. Every listing it
   pulls lands in the same cache the tree reads, so the first search warms the
   tree and the second search is instant. */
const FT_SKIP = new Set(['.git', 'node_modules', '.venv', 'venv', '__pycache__',
  '.mypy_cache', '.ruff_cache', '.pytest_cache', '.cache', 'dist', 'build', 'target', '.next']);
const FT_CRAWL_DIRS = 600;
const FT_CRAWL_PAR = 4;
const FT_SHOW_MAX = 120;

async function ftCrawl(run) {
  FT.crawling = true;
  FT.capped = false;
  let budget = FT_CRAWL_DIRS;
  const queue = [''];
  const seen = new Set(queue);
  const worker = async () => {
    while (queue.length) {
      if (FT.run !== run) return;
      const dir = queue.shift();
      let kids = FT.kids.get(dir);
      if (kids === undefined) {
        if (budget <= 0) { FT.capped = true; continue; }
        budget -= 1;
        kids = await ftFetch(dir);
        if (FT.run === run) ftDrawSoon();
      }
      if (!kids || kids.err) continue;
      kids.forEach((e) => {
        if (!e.dir || FT_SKIP.has(e.name)) return;
        const full = ftJoin(dir, e.name);
        if (!seen.has(full)) { seen.add(full); queue.push(full); }
      });
    }
  };
  await Promise.all(Array.from({ length: FT_CRAWL_PAR }, worker));
  if (FT.run !== run) return;
  FT.crawling = false;
  ftDraw();
}

/* Streams land many at a time; one repaint per frame is enough. */
let ftDrawQueued = false;
function ftDrawSoon() {
  if (ftDrawQueued) return;
  ftDrawQueued = true;
  requestAnimationFrame(() => { ftDrawQueued = false; ftDraw(); });
}

function ftMatches() {
  const out = [];
  const walk = (dir) => {
    const kids = FT.kids.get(dir);
    if (!kids || kids.err) return;
    kids.forEach((e) => {
      if (e.dir && FT_SKIP.has(e.name)) return;
      const full = ftJoin(dir, e.name);
      const at = e.name.toLowerCase().indexOf(FT.q);
      if (at >= 0) out.push({ e, full, at });
      if (e.dir) walk(full);
    });
  };
  walk('');
  /* Name-starts-with beats name-contains, files beat folders (opening one is
     the usual intent), shallow beats deep. */
  const depth = (p) => p.split('/').length;
  out.sort((a, b) => (a.at === 0 ? 0 : 1) - (b.at === 0 ? 0 : 1)
    || (a.e.dir ? 1 : 0) - (b.e.dir ? 1 : 0)
    || depth(a.full) - depth(b.full)
    || a.e.name.localeCompare(b.e.name));
  return out;
}

/* Open every folder above the path, so the file has a row in the tree when the
   query is cleared -- or right now, when revealing is the point. */
function ftOpenTo(full) {
  let acc = '';
  full.split('/').slice(0, -1).forEach((p) => {
    acc = ftJoin(acc, p);
    FT.open.add(acc);
    ftLoad(acc);
  });
}

function ftReveal(full, isDir) {
  ftOpenTo(full);
  if (isDir) { FT.open.add(full); ftLoad(full); }
  FT.q = '';
  FT.run += 1;
  FT.crawling = false;
  const inp = FT.list && FT.list.parentElement && FT.list.parentElement.querySelector('.ftq input');
  if (inp) inp.value = '';
  ftDraw();
  requestAnimationFrame(() => {
    const row = FT.list && FT.list.querySelector(`[data-p="${CSS.escape(full)}"]`);
    if (row) row.scrollIntoView({ block: 'center' });
  });
}

function ftName(e, at) {
  const nm = mk('span', 'nm');
  nm.append(e.name.slice(0, at), mk('b', 'hl', e.name.slice(at, at + FT.q.length)), e.name.slice(at + FT.q.length));
  return nm;
}

function ftResults(box) {
  const hits = ftMatches();
  hits.slice(0, FT_SHOW_MAX).forEach(({ e, full, at }) => {
    const row = mk('button', 'ftrow');
    row.style.paddingLeft = '10px';
    row.appendChild(ico(e.dir ? ICO.file : ICO.doc, 'fi ' + (e.dir ? 'k-dir' : 'k-' + ftKind(e.name))));
    row.appendChild(ftName(e, at));
    const cut = full.lastIndexOf('/');
    if (cut > 0) {
      const pth = mk('span', 'pth', full.slice(0, cut));
      pth.title = full;
      row.appendChild(pth);
    }
    row.onclick = () => {
      if (e.dir) { ftReveal(full, true); return; }
      ftOpenTo(full);
      showFile(ftAbs(full));
    };
    ctxMenu(row, () => (e.dir ? [] : [{ label: T('gui.ws.open'), fn: () => { ftOpenTo(full); showFile(ftAbs(full)); } }])
      .concat([
        { label: T('gui.ws.reveal'), fn: () => ftReveal(full, e.dir) },
        { label: T('gui.ws.copy_path_do'), fn: () => copyToClip(ftAbs(full), T('gui.ws.copy_path')) },
        { label: T('gui.ws.copy_name'), fn: () => copyToClip(e.name, T('gui.ws.copied_name')) },
      ]));
    box.appendChild(row);
  });
  if (!hits.length && !FT.crawling) box.appendChild(ftLeaf(T('gui.ws.no_match'), 0));
  if (hits.length > FT_SHOW_MAX) {
    box.appendChild(ftLeaf(T('gui.ws.search_more', { n: hits.length - FT_SHOW_MAX }), 0));
  }
  if (FT.crawling) box.appendChild(ftLeaf(T('gui.ws.searching'), 0));
  else if (FT.capped) box.appendChild(ftLeaf(T('gui.ws.search_capped'), 0));
}

function ftRows(box, dir, depth) {
  const kids = FT.kids.get(dir);
  if (kids === undefined) {
    ftLoad(dir);
    box.appendChild(ftLeaf(T('gui.ws.file_loading'), depth));
    return;
  }
  if (kids.err) { box.appendChild(ftLeaf(T('gui.ws.read_fail', { err: kids.err }), depth)); return; }
  if (!kids.length) { box.appendChild(ftLeaf(T('gui.ws.dir_empty'), depth)); return; }
  kids.forEach((e) => {
    const full = ftJoin(dir, e.name);
    const open = e.dir && FT.open.has(full);
    const row = mk('button', 'ftrow' + (!e.dir && WS.file && WS.file.path === ftAbs(full) ? ' on' : ''));
    row.dataset.p = full;
    row.style.paddingLeft = (10 + depth * 13) + 'px';
    if (depth) {
      /* Guide lines, one per level, each under its ancestor's chevron. Inline
         because the count is the row's depth; hover keeps working because the
         states set background-color, never the shorthand. */
      row.style.backgroundImage = 'repeating-linear-gradient(to right, var(--line) 0 1px, transparent 1px 13px)';
      row.style.backgroundSize = (depth * 13) + 'px 100%';
      row.style.backgroundPosition = '16px 0';
      row.style.backgroundRepeat = 'no-repeat';
    }
    if (e.dir) {
      row.setAttribute('aria-expanded', String(open));
      row.appendChild(ico(ACT_ICO.chev, 'cv'));
      row.appendChild(ico(ICO.file, 'fi k-dir'));
    } else {
      row.appendChild(mk('span', 'sp'));
      row.appendChild(ico(ICO.doc, 'fi k-' + ftKind(e.name)));
    }
    row.appendChild(mk('span', 'nm', e.name));
    if (!e.dir) row.appendChild(mk('span', 'sz', fmtSize(e.size)));
    const flip = () => {
      if (FT.open.has(full)) FT.open.delete(full); else { FT.open.add(full); ftLoad(full); }
      ftDraw();
    };
    row.onclick = () => { if (e.dir) flip(); else showFile(ftAbs(full)); };
    ctxMenu(row, () => (e.dir
      ? [{ label: T(open ? 'gui.ws.dir_collapse' : 'gui.ws.dir_expand'), fn: flip }]
      : [{ label: T('gui.ws.open'), fn: () => showFile(ftAbs(full)) }]
    ).concat([
      { label: T('gui.ws.copy_path_do'), fn: () => copyToClip(ftAbs(full), T('gui.ws.copy_path')) },
      { label: T('gui.ws.copy_name'), fn: () => copyToClip(e.name, T('gui.ws.copied_name')) },
    ]));
    box.appendChild(row);
    if (e.dir && open) ftRows(box, full, depth + 1);
  });
}

function ftLeaf(text, depth) {
  const d = mk('div', 'ftleaf', text);
  d.style.paddingLeft = (23 + depth * 13) + 'px';
  return d;
}

let ftDebounce = 0;
function ftPane() {
  const tree = mk('div', 'ftree');
  const q = mk('div', 'ftq');
  q.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"'
    + ' aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/></svg>';
  const inp = mk('input');
  inp.type = 'search';
  inp.placeholder = T('gui.ws.search');
  inp.setAttribute('aria-label', T('gui.ws.search'));
  inp.value = FT.q;
  inp.oninput = () => {
    FT.q = inp.value.trim().toLowerCase();
    FT.run += 1;
    clearTimeout(ftDebounce);
    /* What is already cached answers immediately; the crawl for the rest waits
       out the keystroke burst. */
    ftDraw();
    if (FT.q) ftDebounce = setTimeout(() => ftCrawl(FT.run), 220);
    else FT.crawling = false;
  };
  inp.onkeydown = (e) => {
    if (e.key === 'Escape' && inp.value) {
      e.stopPropagation();
      inp.value = '';
      inp.oninput();
    }
  };
  q.appendChild(inp);
  const list = mk('div', 'ftlist');
  FT.list = list;
  tree.append(q, list);
  if (FT.q) ftResults(list); else ftRows(list, '', 0);
  return tree;
}

/* The seam between tree and file. It writes the width straight onto the wrap
   rather than through a redraw: a drag repaints on every pointer move, and
   rebuilding the tree at that rate would drop frames on a deep folder. */
function ftGrip(wrap) {
  const g = mk('button', 'grip');
  g.type = 'button';
  g.setAttribute('role', 'separator');
  g.setAttribute('aria-orientation', 'vertical');
  g.title = T('gui.ws.tree_resize');
  g.setAttribute('aria-label', T('gui.ws.tree_resize'));
  const put = (px, persist) => {
    const max = Math.max(150, (wrap.offsetWidth || 600) - 220);
    FT.w = Math.round(Math.max(150, Math.min(max, px)));
    wrap.style.setProperty('--ftw', FT.w + 'px');
    if (persist) { try { localStorage.setItem(FTW_KEY, String(FT.w)); } catch {} }
  };
  g.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    const x0 = e.clientX;
    const w0 = FT.hide ? 0 : FT.w;
    let opened = false;
    g.dataset.drag = 'true';
    g.setPointerCapture(e.pointerId);
    document.body.style.userSelect = 'none';
    const move = (ev) => {
      const d = ev.clientX - x0;
      /* Dragging the shut seam to the right is how the tree comes back, without
         going up to the folder for it. */
      if (FT.hide) {
        if (d < 60) return;
        FT.hide = false;
        opened = true;
        wrap.dataset.tree = 'on';
      }
      put(w0 + d, false);
    };
    const up = () => {
      g.removeEventListener('pointermove', move);
      g.removeEventListener('pointerup', up);
      g.removeEventListener('pointercancel', up);
      delete g.dataset.drag;
      document.body.style.userSelect = '';
      if (opened) { drawWs(); return; }
      /* Shoved against its own floor: the intent was to get rid of it. */
      if (!FT.hide && FT.w <= 150 && e.clientX - x0 < -40) {
        FT.hide = true;
        drawWs();
        return;
      }
      put(FT.w, true);
    };
    g.addEventListener('pointermove', move);
    g.addEventListener('pointerup', up);
    g.addEventListener('pointercancel', up);
  });
  g.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 40 : 10;
    if (FT.hide) return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); put(FT.w - step, true); }
    if (e.key === 'ArrowRight') { e.preventDefault(); put(FT.w + step, true); }
  });
  return g;
}

const FT_ICO = {
  folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"'
    + ' stroke-linejoin="round" aria-hidden="true"><path d="M3 6.6a2 2 0 0 1 2-2h3.6l1.8 2.2H19a2 2 0 0 1 2 2v8.6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  page: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"'
    + ' aria-hidden="true"><path d="M7 2.8h7L19 8v13.2H7z"/><path d="M13.5 2.8V8H19"/></svg>'
};

/* Where the file sits, as its own line of text: the folders in faint, the file
   itself in the text colour, so the name still reads first. */
function fbarPath(path) {
  const nm = mk('span', 'nm');
  const rel = wsShortPath(path);
  const cut = rel.lastIndexOf('/');
  if (cut > 0) nm.appendChild(mk('i', null, rel.slice(0, cut + 1)));
  nm.appendChild(mk('b', null, cut > 0 ? rel.slice(cut + 1) : rel));
  nm.title = path;
  return nm;
}

drawWsFile = function (box) {
  box.dataset.view = 'file';
  const wrap = mk('div', 'fwrap');
  wrap.dataset.tree = FT.hide ? 'off' : 'on';
  wrap.style.setProperty('--ftw', FT.w + 'px');

  const f = WS.file;
  /* One bar across the whole view, and the folder in it is what opens and shuts
     the tree -- the control belongs next to the path it is about. */
  const bar = mk('div', 'fbar');
  const tog = mk('button', 'ghost-ic ftog tipdn');
  tog.innerHTML = FT_ICO.folder;
  tog.dataset.tip = T(FT.hide ? 'gui.ws.tree_show' : 'gui.ws.tree_hide');
  tog.setAttribute('aria-label', tog.dataset.tip);
  tog.setAttribute('aria-pressed', String(!FT.hide));
  tog.onclick = () => { FT.hide = !FT.hide; drawWs(); };
  bar.appendChild(tog);
  if (f) {
    const nm = fbarPath(f.path);
    ctxMenu(nm, () => [
      { label: T('gui.ws.copy_path_do'), fn: () => copyToClip(f.path, T('gui.ws.copy_path')) },
      { label: T('gui.ws.reveal'), fn: () => ftReveal(relToRoot(f.path) || relToWorkspace(f.path) || f.path, false) },
    ]);
    bar.appendChild(nm);
    /* Copy rides right behind the path it copies -- an icon at the far end of
       the bar read as a generic control for the whole view, not for this line. */
    const cp = mk('button', 'ghost-ic fcopy tipdn');
    cp.appendChild(ico(ICO.doc));
    cp.dataset.tip = T('gui.ws.copy_path_do');
    cp.setAttribute('aria-label', T('gui.ws.copy_path_do'));
    cp.onclick = () => navigator.clipboard && navigator.clipboard.writeText(f.path)
      .then(() => toast(T('gui.ws.copy_path')), () => {});
    bar.appendChild(cp);
  } else {
    bar.appendChild(mk('span', 'nm', T('gui.ws.file_none')));
  }
  bar.appendChild(mk('span', 'fsp'));

  const row = mk('div', 'frow');
  row.appendChild(ftPane());
  row.appendChild(ftGrip(wrap));
  const pane = mk('div', 'fpane');
  if (!f) {
    const empty = mk('div', 'fempty');
    const g = mk('div');
    g.innerHTML = FT_ICO.page;
    empty.append(g, mk('div', 't', T('gui.ws.file_none')));
    pane.appendChild(empty);
    row.appendChild(pane);
    wrap.append(bar, row);
    box.appendChild(wrap);
    return;
  }
  /* Only offered where there are two forms to choose between: a .py has no
     rendered form, and a toggle that cannot change anything is noise. */
  if (RENDERED[f.kind]) {
    const seg = mk('div', 'kseg');
    [[false, 'gui.ws.file_rendered'], [true, 'gui.ws.file_source']].forEach(([raw, key]) => {
      const b = mk('button', null, T(key));
      b.setAttribute('aria-pressed', String(!!f.raw === raw));
      b.onclick = () => { f.raw = raw; drawWs(); };
      seg.appendChild(b);
    });
    bar.appendChild(seg);
  }
  /* A framed document depends on the host's own viewer -- WKWebView renders a
     PDF inline, a stripped Chromium may render nothing -- and the sandbox that
     keeps an artifact off this origin is not negotiable. So the frame stays,
     and this is the way out when the host draws nothing in it. */
  if (f.kind === 'pdf' || f.kind === 'html') {
    const ext = mk('button', 'ghost-ic tipdn');
    ext.appendChild(ico(ICO.ext));
    ext.dataset.tip = T('gui.ws.file_newtab');
    ext.setAttribute('aria-label', T('gui.ws.file_newtab'));
    ext.onclick = () => window.open(fileURL(f.path), '_blank', 'noopener');
    bar.appendChild(ext);
  }
  /* The host-side action lives at the end of the bar: it opens a window on the
     gateway's own desktop, worded for that machine's file manager. */
  const rv = mk('button', 'ghost-ic tipdn');
  rv.appendChild(ico(ICO.reveal));
  rv.dataset.tip = T(HOST_PLATFORM === 'mac' ? 'gui.ws.reveal_finder'
    : HOST_PLATFORM === 'windows' ? 'gui.ws.reveal_explorer' : 'gui.ws.reveal_folder');
  rv.setAttribute('aria-label', rv.dataset.tip);
  rv.onclick = () => rpc.call('fs.reveal', { path: f.path, session: cur || '' })
    .then(() => {}, (e) => toast((e && e.message) || String(e)));
  bar.appendChild(rv);
  /* No close: picking another file replaces this one, and an empty pane is not
     a state anyone asks for. */
  const body = mk('div', 'fbody');
  body.appendChild(fileBody(f));
  pane.appendChild(body);
  row.appendChild(pane);
  wrap.append(bar, row);
  box.appendChild(wrap);
};

wsShortPath = function (p) {
  const rel = relToRoot(p) || relToWorkspace(p);
  return rel || String(p).replace(/^\/Users\/[^/]+\//, '~/');
};

/* Host action for the Changes view: the viewer reads whatever the agent may
   read, so a path outside the workspace opens the same way. */
wsOpenPath = function (p) { showFile(p); };
wsOpenUrl = function (u) {
  if (/^https?:\/\//.test(u)) { window.open(u, '_blank', 'noopener'); return; }
  navigator.clipboard.writeText(String(u)).then(
    () => toast(T('gui.ws.copy_path')),
    () => toast(String(u))
  );
};

