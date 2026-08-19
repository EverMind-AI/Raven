/* ══ boot ═════════════════════════════════════════════════════════
   Each step is isolated: one failure used to abort the whole sequence and
   leave the static shell on screen with no session list, no transcript and
   no clue why. Now a failure is visible and the rest still renders.      */
function bootError(where, e) {
  const bar = document.createElement('div');
  bar.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:99;background:#d96a5b;color:#fff;'
    + 'font:12px/1.5 ui-monospace,monospace;padding:8px 14px;white-space:pre-wrap';
  const at = ((e && e.stack) || '').split('\n')[1] || '';
  bar.textContent = T('gui.boot_fail', { where, err: (e && e.message) || e }) + `\n${at.trim()}`;
  document.body.appendChild(bar);
  if (window.console) console.error('[boot]', where, e);
}

[
  ['lookLoad', () => lookLoad()],
  ['paneLoad', () => paneLoad()],
  ['setRail', () => setRail(true)],
  ['drawList', () => drawList()],
  ['openSession', () => openSession(SESS[0])],
  ['drawCapsBadge', () => drawCapsBadge()],
  ['drawPerm', () => drawPerm()],
  ['drawCtx', () => drawCtx()],
  ['drawCaps', () => drawCaps()],
  ['drawFoot', () => drawFoot()],
  ['bumpWs', () => bumpWs()],
  ['drawSettings', () => drawSettings()],
  ['setRuntime', () => setRuntime('local')],
  ['goState', () => goState()]
].forEach(([where, step]) => {
  try { step(); } catch (e) { bootError(where, e); }
});

/* ══ boot splash + first-run onboarding ══════════════════════════ */

/* The splash is already on screen (it is the first thing in <body>); all the
   page has to do is take it down at the right moment. A floor on its display
   time keeps a fast boot from flashing it for two frames. */
const _spT0 = Date.now();
function hideSplash(minMs) {
  const s = document.getElementById('splash');
  if (!s) return;
  const wait = Math.max(0, (minMs == null ? 600 : minMs) - (Date.now() - _spT0));
  setTimeout(() => {
    s.dataset.off = '1';
    setTimeout(() => s.remove(), 560);
  }, wait);
}

/* First-run flow. `be` is the backend: { options, saveKey, setModel, recheck }.
   Live mode maps these onto the exact RPCs the TUI setup gate drives
   (model.options / model.save_key / config.set key=model / setup.status), so
   the onboarding *logic* exists once, server-side; this function is only the
   face. Resolves after the user is through and the overlay has faded. */
function showOnboard(be) {
  const onb = document.getElementById('onb');
  const box = mk('div', 'ob');
  onb.innerHTML = '';
  onb.appendChild(box);
  onb.hidden = false;
  delete onb.dataset.off;

  let providers = [];
  let sel = null;
  let model = null;

  const DOT_STEPS = ['provider', 'creds', 'model'];
  const dots = (cur) => {
    const d = mk('div', 'ob-dots');
    DOT_STEPS.forEach((n) => {
      const i = mk('i');
      if (n === cur) i.className = 'on';
      d.appendChild(i);
    });
    return d;
  };
  const step = (center) => {
    const s = mk('div', 'ob-step');
    if (center) s.dataset.center = '1';
    box.innerHTML = '';
    box.appendChild(s);
    return s;
  };
  const busy = async (btn, fn) => {
    btn.disabled = true;
    try { return await fn(); } finally { btn.disabled = false; }
  };
  const errOf = (e) => (e && e.data && (e.data.detail || e.data.reason)) || (e && e.message) || String(e);
  const raven = () => {
    const img = mk('img', 'ob-rv');
    img.src = 'assets/ravens/main-agent.webp';
    img.alt = '';
    return img;
  };
  const authBadge = (p) => {
    if (p.authenticated) return mk('span', 'bd on', T('gui.onb.connected'));
    return mk('span', 'bd', p.auth_type === 'oauth' ? 'OAuth' : p.auth_type === 'local' ? 'URL' : 'API Key');
  };

  return new Promise((resolve) => {
    const finish = () => {
      onb.dataset.off = '1';
      setTimeout(() => { onb.hidden = true; onb.innerHTML = ''; resolve(); }, 500);
    };

    function welcome() {
      const s = step(true);
      const btn = mk('button', 'ob-btn', T('gui.onb.start'));
      const err = mk('div', 'ob-err');
      btn.onclick = () => busy(btn, async () => {
        err.textContent = '';
        try {
          const r = await be.options();
          providers = (r && r.providers) || [];
          provider();
        } catch (e) { err.textContent = T('gui.onb.err_generic', { err: errOf(e) }); }
      });
      s.append(raven(), mk('div', 'ob-t', T('gui.onb.welcome_t')), mk('div', 'ob-s', T('gui.onb.welcome_s')), btn, err);
    }

    function provider() {
      const s = step();
      const list = mk('div', 'ob-list');
      providers.forEach((p) => {
        const row = mk('button', 'ob-row');
        row.type = 'button';
        row.append(mk('span', 'nm', p.name || p.slug), authBadge(p));
        row.onclick = () => { sel = p; model = null; (p.authenticated ? modelPick : creds)(); };
        list.appendChild(row);
      });
      const back = mk('button', 'ob-ghost', T('gui.onb.back'));
      back.onclick = welcome;
      s.append(mk('div', 'ob-t', T('gui.onb.provider_t')), mk('div', 'ob-s', T('gui.onb.provider_s')), list, back, dots('provider'));
    }

    function creds() {
      const s = step();
      const err = mk('div', 'ob-err');
      const back = mk('button', 'ob-ghost', T('gui.onb.back'));
      back.onclick = provider;
      const title = mk('div', 'ob-t', T('gui.onb.key_t', { name: sel.name || sel.slug }));

      if (sel.auth_type === 'oauth') {
        /* The one thing the GUI cannot do for the user: OAuth sign-in runs in
           a terminal (`raven provider login ...`), same as the TUI's own
           instruction. Shown verbatim with a copy button, then re-probed. */
        const cmdText = `raven provider login ${sel.slug.replace(/_/g, '-')}`;
        const cmd = mk('div', 'ob-cmd');
        const cp = mk('button', '', T('gui.onb.copy'));
        cp.type = 'button';
        cp.onclick = () => {
          if (navigator.clipboard) navigator.clipboard.writeText(cmdText).catch(() => {});
          cp.textContent = T('gui.onb.copied');
          setTimeout(() => { cp.textContent = T('gui.onb.copy'); }, 1600);
        };
        cmd.append(mk('code', '', cmdText), cp);
        const btn = mk('button', 'ob-btn', T('gui.onb.oauth_check'));
        btn.onclick = () => busy(btn, async () => {
          err.textContent = '';
          try {
            const r = await be.options();
            providers = (r && r.providers) || providers;
            const fresh = providers.find((p) => p.slug === sel.slug);
            if (fresh && fresh.authenticated) { sel = fresh; modelPick(); }
            else err.textContent = T('gui.onb.oauth_wait');
          } catch (e) { err.textContent = T('gui.onb.err_generic', { err: errOf(e) }); }
        });
        s.append(title, mk('div', 'ob-s', T('gui.onb.oauth_s')), cmd, btn, err, back, dots('creds'));
        return;
      }

      const isLocal = sel.auth_type === 'local';
      const needsBase = isLocal || !!sel.needs_api_base;
      const form = mk('div', 'ob-form');
      let keyIn = null;
      let baseIn = null;
      if (!isLocal) {
        keyIn = mk('input', 'ob-in');
        keyIn.type = 'password';
        keyIn.placeholder = T('gui.onb.key_ph');
        keyIn.autocomplete = 'off';
        keyIn.spellcheck = false;
        form.appendChild(keyIn);
      }
      if (needsBase) {
        baseIn = mk('input', 'ob-in');
        baseIn.type = 'text';
        baseIn.placeholder = T('gui.onb.base_ph');
        baseIn.autocomplete = 'off';
        baseIn.spellcheck = false;
        form.appendChild(baseIn);
      }
      const btn = mk('button', 'ob-btn', T('gui.onb.next'));
      const ready = () => (!keyIn || !!keyIn.value.trim()) && (!baseIn || !!baseIn.value.trim());
      btn.disabled = true;
      form.querySelectorAll('input').forEach((i) => i.addEventListener('input', () => { btn.disabled = !ready(); }));
      btn.onclick = () => busy(btn, async () => {
        err.textContent = '';
        try {
          const r = await be.saveKey(sel.slug, keyIn ? keyIn.value.trim() : '', baseIn ? baseIn.value.trim() : '');
          if (r && r.provider) sel = r.provider;
          modelPick();
        } catch (e) { err.textContent = T('gui.onb.err_generic', { err: errOf(e) }); }
      });
      s.append(title, mk('div', 'ob-s', T(isLocal ? 'gui.onb.local_s' : 'gui.onb.key_s')), form, btn, err, back, dots('creds'));
    }

    function modelPick() {
      const s = step();
      const err = mk('div', 'ob-err');
      const models = (sel.models || []).slice();
      const list = mk('div', 'ob-list');
      const btn = mk('button', 'ob-btn', T('gui.onb.finish'));
      btn.disabled = true;
      const draw = (q) => {
        list.innerHTML = '';
        models.filter((m) => !q || m.toLowerCase().includes(q)).forEach((m) => {
          const row = mk('button', 'ob-row');
          row.type = 'button';
          row.appendChild(mk('span', 'nm', m));
          if (m === model) row.dataset.sel = '1';
          row.onclick = () => { model = m; draw(q); btn.disabled = false; };
          list.appendChild(row);
        });
      };
      draw('');
      btn.onclick = () => busy(btn, async () => {
        err.textContent = '';
        try {
          await be.setModel(model, sel.slug);
          if (await be.recheck()) done();
          else err.textContent = T('gui.onb.err_generic', { err: 'provider not configured' });
        } catch (e) { err.textContent = T('gui.onb.err_generic', { err: errOf(e) }); }
      });
      const back = mk('button', 'ob-ghost', T('gui.onb.back'));
      back.onclick = provider;
      const head = [mk('div', 'ob-t', T('gui.onb.model_t')), mk('div', 'ob-s', T('gui.onb.model_s'))];
      if (models.length > 8) {
        const f = mk('input', 'ob-in');
        f.placeholder = T('gui.onb.search_ph');
        f.style.marginTop = '18px';
        f.autocomplete = 'off';
        f.spellcheck = false;
        f.addEventListener('input', () => draw(f.value.trim().toLowerCase()));
        head.push(f);
      }
      s.append(...head, list, btn, err, back, dots('model'));
    }

    function done() {
      const s = step(true);
      s.append(raven(), mk('div', 'ob-t', T('gui.onb.done_t')), mk('div', 'ob-s', T('gui.onb.done_s')));
      setTimeout(finish, 1500);
    }

    welcome();
  });
}

/* Demo: the splash lifts on load; ?onboard=1 previews the first-run flow on
   canned data (a design pass without wiping ~/.raven). Live mode owns both
   moments itself (window.__liveBoot) -- it holds the splash until real data
   has landed and asks setup.status whether onboarding is due. */
addEventListener('load', () => {
  if (window.__liveBoot) return;
  hideSplash(250);
  if (/[?&]onboard=1/.test(location.search)) showOnboard(demoOnbBackend());
});

function demoOnbBackend() {
  const wait = (v, ms) => new Promise((r) => setTimeout(() => r(v), ms == null ? 420 : ms));
  const P = [
    { slug: 'anthropic', name: 'Anthropic', auth_type: 'key', authenticated: false,
      models: ['claude-fable-5', 'claude-opus-5', 'claude-sonnet-5', 'claude-haiku-4-5'] },
    { slug: 'openai', name: 'OpenAI', auth_type: 'key', authenticated: false,
      models: ['gpt-5.2', 'gpt-5.2-mini', 'o5', 'gpt-4.1'] },
    { slug: 'minimax_global', name: 'MiniMax Global', auth_type: 'oauth', authenticated: false,
      models: ['MiniMax-M2.5', 'MiniMax-M2'] },
    { slug: 'deepseek', name: 'DeepSeek', auth_type: 'key', authenticated: false,
      models: ['deepseek-chat', 'deepseek-reasoner'] },
    { slug: 'ollama', name: 'Ollama', auth_type: 'local', needs_api_base: true, authenticated: false,
      models: ['qwen3:32b', 'llama4:70b'] }
  ];
  let pokes = 0;
  return {
    options() {
      /* Second re-probe flips the OAuth row to signed-in, so the preview can
         walk the "not yet -> try again -> through" path once. */
      if (pokes++ >= 2) P.find((p) => p.slug === 'minimax_global').authenticated = true;
      return wait({ model: '', provider: '', providers: P.map((p) => ({ ...p })) });
    },
    saveKey(slug) {
      const p = P.find((x) => x.slug === slug);
      p.authenticated = true;
      return wait({ provider: { ...p } });
    },
    setModel() { return wait({ applied: true }, 600); },
    recheck() { return wait(true, 300); }
  };
}
