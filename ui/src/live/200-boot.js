/* ---- boot ---------------------------------------------------------- */
(async () => {
  if (!(await rpc.connect())) return;
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
    listReady = true;
    drawList();
    /* Home is the new-task screen, never the last session: opening straight
       into someone else's half-finished transcript is a worse first frame
       than an empty composer, and the rail is one click away. A draft writes
       nothing to disk, so this costs no empty session either. */
    startDraft();
    /* First run: the same gate the TUI boots through. setup.status decides;
       the flow itself drives model.options / model.save_key / config.set --
       one server-side setup logic, two faces. Not awaited: the overlay
       resolves on its own while the rest of boot continues underneath.
       ?onboard=1 forces the flow for a design pass on a configured machine.
       Errors leave the gate open (v0.1 fallback, same as the TUI). */
    try {
      const setup = await rpc.call('setup.status', {});
      if (setup.provider_configured === false || /[?&]onboard=1/.test(location.search)) {
        showOnboard({
          options: () => rpc.call('model.options', {}),
          saveKey: (slug, api_key, api_base) => rpc.call('model.save_key', {
            slug,
            ...(api_key ? { api_key } : {}),
            ...(api_base ? { api_base } : {}),
          }),
          setModel: (value, provider) => rpc.call('config.set', { key: 'model', value, ...(provider ? { provider } : {}) }),
          recheck: async () => {
            try { return (await rpc.call('setup.status', {})).provider_configured !== false; }
            catch { return true; }
          },
        });
      }
    } catch (e) {
      if (window.console) console.warn('[live boot] setup.status failed; skipping onboarding gate', e);
    }
    hideSplash();
    shellReady();
    loadSettings().catch(() => {});
    /* Refresh the rail badges from real data right away — until these resolve
       the badges stay suppressed (data-counts="pending") rather than showing
       the demo mock's phantom counts. */
    Promise.allSettled([
      loadExt().then(() => drawCapsBadge()),
      loadCrons().then(() => drawCronBdg()),
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

