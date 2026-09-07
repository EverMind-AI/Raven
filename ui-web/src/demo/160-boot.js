/* ══ boot ═════════════════════════════════════════════════════════
   Declared here but queued after the whole assembled script: in live mode
   every synchronous DS installer must run before the first data-driven paint.
   Each step is isolated so one failure stays visible and the rest still
   renders. */
function bootPage() {
  [
    ['lookLoad', () => lookLoad()],
    ['paneLoad', () => paneLoad()],
    ['setRail', () => setRail(true)],
    ['sessionDraw', () => sessionDraw()],
    /* Live boot deliberately starts with an empty source and chooses a draft
       after the real list lands. Demo mode has a fixture row to open here. */
    ['sessionOpen', () => { const first = sessionRows()[0]; if (first) sessionOpen(first); }],
    ['drawCapsBadge', () => drawCapsBadge()],
    ['drawPerm', () => drawPerm()],
    ['loadTier', () => loadTier()],
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
}

/* The live guard runs later in this same script task and claims boot before
   microtasks drain. Its final part queues bootPage after every source install. */
queueMicrotask(() => { if (!window.__liveBoot) bootPage(); });

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

const { open: showOnboard } = RavenIslands.onboard;

/* Demo boot: two independent moments, and only one of them backs off for live
   mode. Ordered rather than nested, because the preview needs the splash lifted
   and the splash is the half that defers.

   `?onboard=demo` previews the first-run flow on canned data -- it answers from
   demoOnbBackend, so it writes nothing. What was missing was that preview on a
   LIVE page: live/010-boot-guard.js sets __liveBoot whenever the page is served
   over http without ?stub=1, and the old branch backed off on it, so the only
   way to reach a canned first-run screen was file:// or ?stub=1, both of which
   blank the live data as well. Against a real serve there was no way to look at
   that flow without letting it write.

   `?onboard=1` still means what it always meant: force the real flow, through
   live/200-boot.js, against the real RPCs. That one writes.

   The splash defers to live, which holds it until real data lands. It must NOT
   be skipped when the preview opens, though: #onb is z-index 110 and #splash is
   120 (page.css says so where it stacks them), and on the offline canvas --
   file:// or ?stub=1, which is what build.py's docstring points a design pass at
   -- no live layer runs, so this is the only call that ever lifts it. Returning
   early here left the splash over the overlay for the whole session. */
DS.onboard ??= demoOnbBackend();

addEventListener('load', () => {
  if (/[?&]onboard=demo/.test(location.search)) showOnboard();
  if (window.__liveBoot) return;
  hideSplash(250);
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
