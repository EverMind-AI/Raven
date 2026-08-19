/* -- tabs -------------------------------------------------------------- */

const brNoFav = new Set();  // origins whose /favicon.ico 404'd -- don't re-ask every strip redraw

function brTabStrip() {
  const strip = document.querySelector('#wsBody .btabs');
  if (!strip) return;
  strip.innerHTML = '';
  BR.tabs.forEach((t) => {
    const tab = mk('div', 'btab');
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-current', String(!!t.active));
    tab.title = t.title || t.url;
    if (t.loading) tab.appendChild(mk('span', 'ld'));
    else {
      let host = '', origin = '';
      try { const u = new URL(t.url); host = u.host; origin = u.origin; } catch { /* about:blank */ }
      if (host && !brNoFav.has(origin)) {
        const fav = document.createElement('img');
        fav.className = 'fav';
        fav.alt = '';
        fav.src = `${origin}/favicon.ico`;
        fav.onerror = () => {
          brNoFav.add(origin);
          const l = mk('span', 'fav ltr', (host[0] || '?').toUpperCase());
          fav.replaceWith(l);
        };
        tab.appendChild(fav);
      } else if (host) {
        tab.appendChild(mk('span', 'fav ltr', (host[0] || '?').toUpperCase()));
      } else {
        tab.appendChild(mk('span', 'fav ltr', '·'));
      }
    }
    const label = t.title || t.url.replace(/^https?:\/\//, '') || T('gui.br.tab_blank');
    tab.appendChild(mk('span', 'tt', label));
    const x = mk('button', 'bx', '✕');
    x.title = T('gui.br.tab_close');
    x.setAttribute('aria-label', T('gui.br.tab_close'));
    x.onclick = (e) => { e.stopPropagation(); brTabs('close', { index: t.index }); };
    tab.appendChild(x);
    tab.onclick = () => { if (!t.active) brTabs('activate', { index: t.index }); };
    tab.onauxclick = (e) => { if (e.button === 1) { e.preventDefault(); brTabs('close', { index: t.index }); } };
    strip.appendChild(tab);
  });
  const add = mk('button', 'btab-new', '+');
  add.title = T('gui.br.tab_new');
  add.setAttribute('aria-label', T('gui.br.tab_new'));
  add.onclick = () => brTabs('new', {});
  strip.appendChild(add);
}

let brTabsBusy = false;
async function brTabsSync() {
  if (!brShowing() || !BR.started || BR.headful || brTabsBusy) return;
  brTabsBusy = true;
  try {
    const r = await rpc.call('browser.tabs', { action: 'list' });
    BR.tabs = r.tabs || [];
    brTabStrip();
  } catch { /* no browser.tabs here: the strip just stays empty */ }
  brTabsBusy = false;
}

async function brTabs(action, extra) {
  try {
    const r = await rpc.call('browser.tabs', Object.assign({ action }, extra));
    BR.tabs = r.tabs || [];
    if (!r.started) { BR.started = false; BR.lastBlob = null; BR.url = ''; drawWs(); return; }
    if (r.url !== undefined) BR.url = r.url || BR.url;
    brStateIn(r);
    brTabStrip();
    const bar = document.querySelector('#wsBody .bbar .url');
    if (bar && document.activeElement !== bar) bar.value = BR.url;
    if (action === 'new') {
      const u = document.querySelector('#wsBody .bbar .url');
      if (u) { u.focus(); u.select(); }
    }
    brWatch(true);
    /* A static page repaints nothing after a tab switch, so the stream has no
       frame to push -- pull one so the stage shows the tab we just went to. */
    if (action === 'activate' || action === 'new') brPoll(true);
  } catch (e) { BR.err = e.message || String(e); }
}

function brTabTick(on) {
  if (BR.tabTimer) { clearInterval(BR.tabTimer); BR.tabTimer = null; }
  if (on) BR.tabTimer = setInterval(brTabsSync, 2500);
}

/* The viewport tracks the stage, so this is normally 1:1 -- the math only
   earns its keep in the beat between a resize and the restream, when the
   frame is letterboxed (object-fit: contain) inside the new stage. */
function brToPage(stage, e) {
  const r = stage.getBoundingClientRect();
  const vw = BR.vp[0] || r.width || 1;
  const vh = BR.vp[1] || r.height || 1;
  const s = Math.min(r.width / vw, r.height / vh) || 1;
  const ox = r.left + (r.width - vw * s) / 2;
  const oy = r.top + (r.height - vh * s) / 2;
  return {
    x: Math.round(Math.min(vw, Math.max(0, (e.clientX - ox) / s))),
    y: Math.round(Math.min(vh, Math.max(0, (e.clientY - oy) / s))),
  };
}

function brInput(payload) {
  rpc.call('browser.input', payload).catch((e) => { BR.err = e.message || String(e); });
  /* Pushed frames show the result on their own; only the poll fallback needs
     to go and look. */
  if (BR.noWatch) setTimeout(() => brPoll(true), 350);
}

drawWsWeb = function (box) {
  if (BR.avail === null) { brPoll(true).then(() => { if (brShowing()) drawWs(); }); }

  /* Absent is not the same as uninstalled, and the difference is the whole
     point of telling the reader anything: chromium is worth an install command,
     a server without the surface is not -- following that command would change
     nothing. */
  if (!rpcHas('browser')) {
    const n = mk('div', 'bnote');
    n.append(mk('div', 'h', T('gui.br.absent_h')), mk('div', 'w', T('gui.br.absent')));
    box.appendChild(n);
    return;
  }

  if (BR.avail === false) {
    const n = mk('div', 'bnote');
    n.append(mk('div', 'h', T('gui.br.unavail')), mk('div', 'w', T('gui.br.unavail_w')));
    n.appendChild(mk('code', null, 'uv sync --extra browser && uv run playwright install chromium'));
    if (BR.reason) n.appendChild(mk('div', 'w', BR.reason));
    box.appendChild(n);
    return;
  }

  if (BR.started && !BR.headful) {
    box.appendChild(mk('div', 'btabs'));
    brTabStrip();
    brTabsSync();
    brTabTick(true);
  } else {
    brTabTick(false);
  }

  const bar = mk('div', 'bbar');
  const nav = mk('div', 'nav');
  const navBtn = (cls, glyph, key, fn, rotate) => {
    const b = mk('button', 'ghost-ic ' + cls);
    const g = ico(glyph);
    if (rotate) g.style.transform = 'rotate(180deg)';
    b.appendChild(g);
    b.title = T(key);
    b.setAttribute('aria-label', T(key));
    b.onclick = fn;
    nav.appendChild(b);
    return b;
  };
  const back = navBtn('bk', ICO.up, 'gui.br.back', () => brGo('back'));
  const fwd = navBtn('fw', ICO.up, 'gui.br.forward', () => brGo('forward'), true);
  back.disabled = !BR.started || !BR.canBack;
  fwd.disabled = !BR.started || !BR.canFwd;
  const rl = navBtn(
    'brl',
    BR.loading ? 'M6 6l12 12M18 6L6 18' : 'M4.5 12a7.5 7.5 0 1 0 2.6-5.7M4.5 5.5V10h4.5',
    BR.loading ? 'gui.br.stop' : 'gui.br.reload',
    () => brGo(BR.loading ? 'stop' : 'reload'),
  );
  rl.disabled = !BR.started;
  bar.appendChild(nav);

  /* Address field: security glyph inside, select-all on focus, Esc restores,
     and Enter routes -- URL-shaped input navigates, anything else searches. */
  const wrap = mk('span', 'burl');
  const sec = mk('span', 'sec');
  wrap.appendChild(sec);
  const url = mk('input', 'url');
  url.type = 'text';
  url.value = BR.url;
  url.placeholder = T('gui.br.url_ph');
  url.setAttribute('aria-label', T('gui.br.url_ph'));
  url.autocomplete = 'off';
  url.spellcheck = false;
  url.onfocus = () => url.select();
  url.onkeydown = (e) => {
    if (e.key === 'Escape') { url.value = BR.url; url.blur(); return; }
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const v = url.value.trim();
    if (!v) return;
    const urlish = /^[a-z][a-z0-9+.-]*:\/\//i.test(v)
      || (!/\s/.test(v) && (/^localhost(:\d+)?([/?#]|$)/i.test(v) || /^[\w-]+(\.[\w-]+)+/.test(v) || /^\d{1,3}(\.\d{1,3}){3}/.test(v)));
    const search = LANG === 'zh'
      ? `https://www.baidu.com/s?wd=${encodeURIComponent(v)}`
      : `https://duckduckgo.com/?q=${encodeURIComponent(v)}`;
    brOpen(urlish ? v : search);
    url.blur();
  };
  wrap.appendChild(url);
  bar.appendChild(wrap);

  const prog = mk('div', 'bprog');
  prog.appendChild(mk('i'));
  prog.hidden = !BR.loading;
  bar.appendChild(prog);

  if (BR.started && !BR.headful) {
    const pop = mk('button', 'ghost-ic');
    pop.appendChild(ico('M9 5H5v14h14v-4M14 4h6v6M20 4l-9 9'));
    pop.title = T('gui.br.popout');
    pop.setAttribute('aria-label', T('gui.br.popout'));
    pop.onclick = async () => {
      brWatch(false);
      try { const r = await rpc.call('browser.mode', { headful: true }); BR.headful = !!r.headful; } catch { /* poll reports it */ }
      drawWs();
    };
    bar.appendChild(pop);
  }
  if (BR.started) {
    const x = mk('button', 'ghost-ic');
    x.appendChild(ico('M6 6l12 12M18 6L6 18'));
    x.title = T('gui.br.close');
    x.setAttribute('aria-label', T('gui.br.close'));
    x.onclick = async () => {
      await rpc.call('browser.close', {});
      BR.started = false; BR.lastBlob = null; BR.url = '';
      drawWs();
    };
    bar.appendChild(x);
  }
  box.appendChild(bar);

  /* Popped out: the page lives in a real Chromium window. The panel keeps the
     address bar working and offers the way back; streaming a window the
     reader can already see would just be a second, worse copy of it. */
  if (BR.started && BR.headful) {
    brTick(true);
    const n = mk('div', 'bnote');
    n.append(mk('div', 'h', T('gui.br.popped')), mk('div', 'w', T('gui.br.popped_w')));
    const back = mk('button', 'btn');
    back.textContent = T('gui.br.popin');
    back.onclick = async () => {
      try { const r = await rpc.call('browser.mode', { headful: false }); BR.headful = !!r.headful; } catch { /* poll reports it */ }
      drawWs();
    };
    n.appendChild(back);
    box.appendChild(n);
    return;
  }

  if (!BR.started) {
    /* Poll cheaply until a page exists -- the agent may open one any moment,
       and the flip to started is what swaps this note for the live stage. */
    brTick(true);
    const n = mk('div', 'bnote');
    n.append(mk('div', 'h', T('gui.br.idle')), mk('div', 'w', T('gui.br.idle_w')));
    if (BR.err) n.appendChild(mk('div', 'w', BR.err));
    /* The pages the agent already fetched are the likeliest thing to want
       open, so the old link list stays -- as a starting point, not the view. */
    if (WS.urls.length) {
      const list = mk('div', 'urls');
      WS.urls.slice(0, 8).forEach((u) => {
        const r = mk('button', 'urow');
        r.appendChild(ico(ICO.web));
        r.appendChild(mk('span', 'u', u.url.replace(/^https?:\/\//, '')));
        r.title = u.url;
        r.onclick = () => brOpen(u.url);
        list.appendChild(r);
      });
      n.appendChild(list);
    }
    box.appendChild(n);
    return;
  }

  /* Live: the stage owns the rest of the panel (flex column, no scroll) and
     the page's viewport is resized to the stage, so pointer geometry is 1:1. */
  box.dataset.view = 'web';
  const stage = mk('div', 'bstage');
  const img = mk('canvas', 'shot');
  img.width = BR.vp[0]; img.height = BR.vp[1];
  img.setAttribute('role', 'img');
  img.setAttribute('aria-label', BR.title || BR.url);

  /* Keystrokes land in an invisible input, not on the stage div: that is what
     lets an IME compose (Chinese input has no keydown spelling), and the
     composed string is forwarded whole. */
  const kb = mk('input', 'kbsink');
  kb.type = 'text';
  kb.autocapitalize = 'off';
  kb.autocomplete = 'off';
  kb.spellcheck = false;
  kb.setAttribute('aria-hidden', 'true');
  kb.tabIndex = -1;
  let composing = false;
  kb.oncompositionstart = () => { composing = true; };
  kb.oncompositionend = (e) => {
    composing = false; kb.value = '';
    if (e.data) brInput({ kind: 'text', text: e.data });
  };
  kb.oninput = () => {
    if (composing) return;
    const v = kb.value; kb.value = '';
    if (v) brInput({ kind: 'text', text: v });
  };
  kb.onkeydown = (e) => {
    if (composing) return;
    if (e.metaKey || e.ctrlKey) {
      const k = e.key.toLowerCase();
      /* The browser's own chrome shortcuts, same keys as the native thing:
         L focuses the address bar, R reloads, T/W manage tabs, [ ] walk
         history. Everything else editing-shaped belongs to the page. */
      if (k === 'l') {
        e.preventDefault();
        const u = document.querySelector('#wsBody .bbar .url');
        if (u) { u.focus(); u.select(); }
        return;
      }
      if (k === 'r') { e.preventDefault(); brGo(BR.loading ? 'stop' : 'reload'); return; }
      if (k === 't') { e.preventDefault(); brTabs('new', {}); return; }
      if (k === 'w') {
        e.preventDefault();
        const act = BR.tabs.find((t) => t.active);
        if (act) brTabs('close', { index: act.index });
        return;
      }
      if (k === '[') { e.preventDefault(); brGo('back'); return; }
      if (k === ']') { e.preventDefault(); brGo('forward'); return; }
      /* Editing shortcuts belong to the page (select-all in its text field,
         not ours). Paste is the exception: letting Cmd+V land in the sink
         turns it into an input event, which is already the IME text path. */
      if (k.length === 1 && 'aczyx'.includes(k)) {
        e.preventDefault();
        brInput({ kind: 'key', key: 'ControlOrMeta+' + (e.shiftKey ? 'Shift+' : '') + k.toUpperCase() });
      }
      return;
    }
    if (e.key.length === 1) return;
    e.preventDefault();
    const mods = [];
    if (e.shiftKey) mods.push('Shift');
    if (e.altKey) mods.push('Alt');
    brInput({ kind: 'key', key: mods.concat(e.key).join('+') });
  };
  kb.onfocus = () => stage.classList.add('hot');
  kb.onblur = () => stage.classList.remove('hot');

  const btnOf = (e) => (e.button === 2 ? 'right' : e.button === 1 ? 'middle' : 'left');
  stage.onpointerdown = (e) => {
    e.preventDefault();
    try { stage.setPointerCapture(e.pointerId); } catch { /* gone mid-gesture */ }
    kb.focus({ preventScroll: true });
    brInput(Object.assign({ kind: 'down', button: btnOf(e), count: e.detail || 1 }, brToPage(stage, e)));
  };
  stage.onpointerup = (e) => {
    brInput(Object.assign({ kind: 'up', button: btnOf(e), count: e.detail || 1 }, brToPage(stage, e)));
  };
  /* Moves coalesce per animation frame -- the newest position wins -- instead
     of a fixed 30ms gate that adds up to a visible rubber-band on drags. */
  let mvPend = null, mvRaf = 0;
  stage.onpointermove = (e) => {
    mvPend = brToPage(stage, e);
    if (mvRaf) return;
    mvRaf = requestAnimationFrame(() => {
      mvRaf = 0;
      if (mvPend) { brInput(Object.assign({ kind: 'move' }, mvPend)); mvPend = null; }
    });
  };
  /* The remote page's own menu is not reachable from here, and the window's
     right-click rule already opens nothing over a canvas that declares no
     actions -- so this surface needs no handler of its own. */
  /* Raw deltas, not rounded: trackpad momentum is made of fractional steps,
     and rounding them is what makes scrolling feel notchy.

     The canvas also shifts locally on the spot -- the exposed band is a smear
     of the edge row until Chromium's next frame lands and repaints the truth.
     That round-trip is exactly the lag that makes a streamed page feel like
     it is dragging; scrolling is the one gesture whose visual result is
     predictable enough to fake for a frame or two. */
  stage.onwheel = (e) => {
    e.preventDefault();
    brInput({ kind: 'wheel', dx: e.deltaX, dy: e.deltaY });
    const cv = stage.querySelector('canvas.shot');
    if (!cv || !cv.width || !BR.vp[1]) return;
    const d = Math.max(-cv.height, Math.min(cv.height, Math.round(e.deltaY * (cv.height / BR.vp[1]))));
    if (!d) return;
    const c2 = cv.getContext('2d'); const w = cv.width; const h = cv.height; const a = Math.abs(d);
    if (d > 0) {
      c2.drawImage(cv, 0, a, w, h - a, 0, 0, w, h - a);
      c2.drawImage(cv, 0, h - 1, w, 1, 0, h - a, w, a);
    } else {
      c2.drawImage(cv, 0, 0, w, h - a, 0, a, w, h - a);
      c2.drawImage(cv, 0, 0, w, 1, 0, 0, w, a);
    }
  };

  stage.appendChild(img);
  stage.appendChild(kb);
  if (BR.lastBlob) brPaint(BR.lastBlob);
  else stage.appendChild(mk('div', 'waiting', T('gui.br.waiting')));
  /* A navigation that failed gets a page, not a status-line mumble. */
  if (BR.err) {
    const err = mk('div', 'berr');
    err.append(mk('div', 'h', T('gui.br.error')), mk('div', 'w', BR.err));
    const retry = mk('button', 'mini gold', T('gui.plug.retry'));
    retry.onclick = () => { BR.err = ''; if (BR.lastTried) brOpen(BR.lastTried); else drawWs(); };
    const dismiss = mk('button', 'mini ghost', T('gui.cancel'));
    dismiss.onclick = () => { BR.err = ''; drawWs(); };
    const row = mk('div');
    row.style.cssText = 'display:flex;gap:8px';
    row.append(retry, dismiss);
    err.appendChild(row);
    stage.appendChild(err);
  }
  box.appendChild(stage);
  brSecSync();

  brWatch(true);
  /* A dragged seam or window resize means a new viewport: re-lease with the
     new size once it settles, and the server restreams at that size. */
  if (!BR.ro) {
    BR.ro = new ResizeObserver(() => {
      clearTimeout(BR.rsz);
      BR.rsz = setTimeout(() => { if (brShowing() && BR.started) brWatch(true); }, 250);
    });
  }
  BR.ro.disconnect();
  BR.ro.observe(stage);
};

/* A link in the transcript opens in the embedded browser -- the panel IS this
   app's browser, same as the built-in viewer is its file opener. Modifier
   clicks (Cmd/Ctrl/Shift/Alt) keep the system-browser escape hatch.

   Only when this server has a browser to open it with. Cancelling the
   navigation first and finding out afterwards is how a plain click on a link
   in a reply became a click that does nothing: the panel opened, `browser.open`
   answered -32601, and the reader was left with a parked error. Unknown counts
   as absent here -- a link opening in the real browser is a worse outcome than
   nothing only if you already know the panel works. */
document.addEventListener('click', (e) => {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  if (!rpcHas('browser') || BR.avail !== true) return;
  const a = e.target && e.target.closest ? e.target.closest('#scroll a[href]') : null;
  if (!a || !/^https?:/i.test(a.href)) return;
  e.preventDefault();
  if (!wsOpen) setWs(true);
  wsPick('browser');
  brOpen(a.href);
}, true);

/* ── subagents: what this conversation handed off ──────────────────────
   Scoped to the open session on every call, so background work from another
   conversation can never surface here. */
/* The rows, and only the rows: which of them is on screen, when to ask again
   and whether the answer is worth a repaint are all about what is drawn, and
   they live with the renderer in demo/110-subagents.js.

   A server without the surface answers -32601 forever otherwise, and the panel
   would sit empty with no way to tell an empty list from a missing feature --
   so an absent surface answers with no rows rather than an error, and
   `rpcAbsent` is what the empty state reads to tell the two apart. */
DS.agents = {
  list: (sessionId) => {
    if (!rpcHas('subagent')) return Promise.resolve([]);
    return rpc.call('subagent.list', { session_id: sessionId })
      .then((r) => (r && r.items) || [])
      /* Absent surface -> no rows (the shell words that empty state); a call
         that merely failed rethrows, so the page keeps what it last drew --
         a dropped socket must not repaint a live run as "no delegated work". */
      .catch((e) => { if (rpcGone('subagent', e)) return []; throw e; });
  },
};

/* One fingerprint per drawn detail: the poll repaints only when the answer
   actually changed, because a redraw every tick reads as flicker and eats any
   text selection the reader had. */
/* What has already been drawn for the open run. `drawn` counts *messages*, not
   nodes: a poll appends the ones that arrived since the last, and never touches
   what is on screen. Rebuilding the whole transcript every five seconds -- which
   is what a running record's poll used to do -- tore out whatever the reader was
   in the middle of, closed every fold they had opened, and dropped the selection.
   The scroll survived it and nothing else did. */
const agentDrawn = { key: null, drawn: 0, status: null };

function agentPaint(box, r, opts) {
  const msgs = (r && r.messages) || [];
  const running = r && r.status === 'run';
  const key = (opts && opts.key) || `sp:${agentOpen}`;
  const fresh = agentDrawn.key !== key;
  if (fresh) { box.innerHTML = ''; agentDrawn.key = key; agentDrawn.drawn = 0; }
  agentDrawn.status = r && r.status;

  /* An answer being streamed is held back until it settles: drawing it is what
     would force a redraw of it a moment later, and it is why an answer in
     flight carries no footer here, exactly as a live turn in the transcript
     carries none until it settles.
     Only an *assistant* message though. A user prompt is never rewritten, and
     for most of a run it is the only message there is -- holding it back drew
     the working glyph over an empty panel, so a reader watching a run could
     not tell which run they were watching. */
  const last = msgs[msgs.length - 1];
  const streaming = running && last && last.role === 'assistant';
  const commit = streaming ? msgs.length - 1 : msgs.length;
  const tail = msgs.slice(agentDrawn.drawn, commit);

  const sc = box;
  const atEnd = sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 4;
  const top = sc.scrollTop;

  /* The working glyph is the tail of the box, so it is kept aside while
     messages are appended and put back after -- kept, not rebuilt: re-creating
     it restarts its CSS animation, and this paint runs on every poll, so a
     fresh glyph each time is a glyph that never finishes a cycle. The turn's
     own live row learned this first; see `drawTurnLive`. */
  const glyph = box.querySelector(':scope > .sarun');
  const empty = box.querySelector(':scope > .wsempty');
  if (empty) empty.remove();

  /* What it did, between what was asked and what came back -- only for the
     lanes whose record holds call names and nothing else; a real transcript
     carries its own rows. It is a whole-record shape, so it is drawn once, on
     the first paint. */
  const did = fresh ? agentFlatCalls(r) : [];
  if (tail.length || did.length) {
    /* The one line that makes this a subagent view rather than a second
       renderer: point the transcript's write target at this box, draw with
       the ordinary one, then put it back. Synchronous, so no live event can
       land in between. */
    const prev = stageHost;
    stageHost = box;
    try {
      if (did.length) {
        renderHistory(tail.filter((m) => m && m.role === 'user'));
        agentDidStep(did);
        renderHistory(tail.filter((m) => !(m && m.role === 'user')));
        agentFoldTime(box, r);
      } else {
        renderHistory(tail);
      }
    } finally { stageHost = prev; }
    agentDrawn.drawn = commit;
  }

  if (running) {
    /* The glyph alone, as the reader's own turn wears it. The word beside it
       ("Working") named the one thing the moving glyph already says, in a panel
       whose header carries the status too -- three ways of saying running, and
       the only one that survives a glance is the movement. */
    let w = glyph;
    if (!w) {
      w = mk('div', 'act in sarun');
      w.append(workGlyph());
    }
    if (box.lastElementChild !== w) box.appendChild(w);
  } else if (glyph) {
    glyph.remove();
  }
  if (!box.childElementCount) {
    box.appendChild(mk('div', 'wsempty', (opts && opts.empty) || T('gui.ws.agents_none')));
  }
  box.scrollTop = atEnd ? box.scrollHeight : top;
}

agentRender = (box, id) => {
  /* Which panel asked. The header below is reached through the document rather
     than through `box`, so an answer for a run the reader has already left
     would find whichever header is open now and rename it. The rest of this
     callback is safe on its own -- it writes into a detached box -- but a
     write to the live document has to know it is still the right document. */
  const epoch = wsEpoch;
  /* A call is addressed by conversation and call, not by call alone: its record
     lives inside that conversation's own directory. */
  agentDrawn.key = null;
  rpc.call('subagent.context', { id, session_id: cur })
    .then((r) => {
      if (wsStale(epoch)) return;
      /* The header drew from the listed row. Opened without one -- a reopened
         panel, a run that has aged out of the list -- it fell back to raven's
         own sub-agent, which would quietly mislabel an openclaw run. The answer
         carries the truth, so correct it on arrival. */
      const who = document.querySelector('.sahd .trow .who');
      if (who && r) who.textContent = (r.agent || 'raven');
      agentPaint(box, r);
    })
    .catch((e) => {
      box.innerHTML = '';
      box.appendChild(mk('div', 'wsempty', (e && e.message) || String(e)));
    });
};

/* A run in flight has to move on screen without being reopened, and its header
   has to change the moment the run does -- a detail page still saying "working"
   over a run the list already knows failed is the panel lying. */
setInterval(() => {
  if (!wsOpen || wsTab !== 'agents') return;
  if (!agentOpen && !dagNode) { agentsRefresh(); return; }
  agentsRefresh(true);
  /* A graph node is a row like any other now, so the same rule applies. It is
     found by (run, node) rather than by id because that is how the list
     addresses it. */
  const it = dagNode
    ? AGENTS.find((a) => a.kind === 'dag' && a.run_id === dagNode.run_id && a.node === dagNode.node)
    : AGENTS.find((a) => a.id === agentOpen);
  const stNow = it ? it.status : null;
  if (agentDrawn.status && stNow && agentDrawn.status !== stNow) {
    /* Status flipped under an open page: redraw the whole view once, so the
       header mark and the transcript land on the final state together. */
    agentDrawn.status = stNow;
    drawWs();
    return;
  }
  if (!it || it.status !== 'run') return;
  const box = document.querySelector('.satx');
  if (!box) return;
  if (dagNode) { dagNodePaint(box, dagNode); return; }
  rpc.call('subagent.context', { id: agentOpen, session_id: cur })
    .then((r) => {
      if (!document.body.contains(box)) return;
      agentPaint(box, r);
    })
    .catch(() => {});
}, 2000);

/* Leaving the browser view -- another tab, another session, the panel shut --
   must drop the watch, and every one of those paths repaints the panel. */
const drawWsBase = drawWs;
drawWs = function () {
  const body = document.querySelector('#wsBody');
  if (body) delete body.dataset.view;
  drawWsBase();
  if (!brShowing()) { brWatch(false); brTick(false); }
};

