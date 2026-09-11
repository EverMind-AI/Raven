/* ---- boot ---------------------------------------------------------- */
DS.onboard = {
  options: () => rpc.call('model.options', {}),
  saveKey: (slug, api_key, api_base) => rpc.call('model.save_key', {
    slug,
    ...(api_key ? { api_key } : {}),
    ...(api_base ? { api_base } : {}),
  }),
  setModel: (value, provider) => rpc.call('config.set', { key: 'model', value, provider }),
  recheck: async () => {
    try { return (await rpc.call('setup.status', {})).provider_configured !== false; }
    catch { return true; }
  },
};

(async () => {
  /* Ahead of the connect, because the failure path below never reaches
     `loadLang`: the notice that explains a page which cannot connect has to
     be in the reader's language, and the only copy available offline is the
     one the last successful boot remembered. */
  langRestore();
  /* The first connect is the one place where a socket that never opened really
     does mean the session is not welcome: nothing has been served to this page
     yet that could have come from a gateway which then went away. The rejoin
     path decides differently, and has to -- see rpc.rejoin. */
  if (!(await rpc.connect())) { authFail(); return; }
  try {
    const hello = await rpc.call('system.hello', { client_version: '0.1.0', surface: SURFACE });
    if (hello && hello.platform) HOST_PLATFORM = hello.platform;
    // Before the first paint of anything data-driven: config.language decides
    // what every label below says.
    await loadLang();
    const v = await rpc.call('system.version', {});
    if (v.raven_version) APP_VERSION = v.raven_version;
    drawFoot();
    /* Absent until system.version carries them; the row simply stays hidden,
       so an older server degrades to no notice rather than a broken one. */
    if (v.update_available) showUpNote('ver', v.latest_version);
    /* That answer came from the update cache, which the gateway refreshes on a
       poll -- so between a publish and the next poll it names a version that is
       already superseded, and the banner promises one build while the button
       installs whatever is newest at click time. Ask again with `check: true`,
       after the paint and deliberately not awaited, so the number the reader is
       shown is the number they will get. */
    rpc.call('system.version', { check: true })
      .then((fresh) => { if (fresh && fresh.update_available) showUpNote('ver', fresh.latest_version); })
      .catch(() => {});
    await loadSessions();
    RavenIslands.rail.release();
    /* Home is the new-task screen, never the last session: opening straight
       into someone else's half-finished transcript is a worse first frame than
       an empty composer, and the rail is one click away. A draft writes nothing
       to disk, so this costs no empty session either.

       A RELOAD is not a first frame, though. The reader was already in a
       conversation and did not ask to leave it -- the page was replaced under
       them, by a refresh or by an upgrade -- so the tab's own note is what
       decides here, and it exists only for a tab that was already somewhere
       (shell/resume.ts). Asked of the list rather than opened blind: a
       conversation deleted since is a note for something that is not there any
       more, and the new-task screen is the right answer for it. */
    const back = RavenIslands.view.landing(sessionRows().map((s) => s.id));
    if (back) await openLiveSession(sess(back));
    else startDraft();
    /* Remember whether task actions need to send the reader to Models. The
       page itself stays available on first run; ?onboard=1 retains the
       standalone onboarding flow for an explicit design or support pass. */
    try {
      const setup = await rpc.call('setup.status', {});
      providerConfiguredLive = setup.provider_configured !== false;
      /* ?onboard=demo asked for the canned flow, which the demo shell has
         already put on screen. Both write into #onb, so opening this one would
         replace it -- and the reader who asked for the version that writes
         nothing would get the version that writes. */
      const cannedInstead = /[?&]onboard=demo/.test(location.search);
      if (!cannedInstead && /[?&]onboard=1/.test(location.search)) {
        showOnboard();
      }
    } catch (e) {
      if (window.console) console.warn('[live boot] setup.status failed; skipping onboarding gate', e);
    }
    hideSplash();
    shellReady();
    /* pushPermMode after the load, same as the settings island's two callers:
       the chip must reflect the server mode on cold boot, not the localStorage
       cache -- the gate enforces the server's answer either way. */
    loadSettings().then(pushPermMode).catch(() => {});
    /* The tier chip's first read. `session.onChange` covers every switch after
       this, but not the state the page boots into: a page with no conversation
       restored never changes session, so the chip would stay hidden on the one
       screen where the reader is about to start a conversation. */
    loadTier();
    /* Refresh the rail badges from real data right away — until these resolve
       the badges stay suppressed (data-counts="pending") rather than showing
       the demo mock's phantom counts. */
    Promise.allSettled([
      loadExt().then(() => drawCapsBadge()),
      cronWarm(),
    ]).then(() => {
      const r = document.querySelector('.rail');
      if (r) delete r.dataset.counts;
    });
    watchForUpdates();
    resumeUpgrade();
  } catch (e) {
    bootFail(e);
  }
})();
