/* ══ typed values in prose ═══════════════════════════════════════════
   An answer carries several kinds of value, and they are not the same thing to
   a reader: a URL is somewhere to go, a path is a file to look at, a command is
   text to copy. Each gets the affordance its kind earns and nothing more.

   The renderer is ui-web/src/shell/prose.ts and the click is ui-web/src/shell/chips.ts.
   What stays here is the offline demo's half of DS.prose: which strings resolve
   to something openable in a page with no server behind it, and what happens
   when the reader clicks one.

   The rule for a path is that it is only ever clickable when it can actually be
   opened, and offline the only honest test is the shape of the string, so that
   is what these do -- enough for the canned transcript to read properly. The
   live layer installs its own source (live/170-workspace.js), which can prove
   the file exists. A path that merely looks like one stays plain text: a dead
   link is worse than no link. */
const demoPathOf = (s) => {
  const t = String(s).trim().replace(/:\d+(?::\d+)?$/, '');
  if (!/^[\w.@+-]+(?:\/[\w.@+-]+)+$/.test(t)) return null;
  return /\.\w{1,8}$/.test(t) ? t : null;
};

/* What a markdown link's local target resolves to: {p, dir} or null. Only for
   links the author wrote -- a bare word in prose gets no benefit of the doubt,
   while [打开交付文件夹](path/) is the author saying "this opens". A target
   without an extension is a folder; that is the whole dir test. */
const demoLinkTargetOf = (u) => {
  const t = String(u).trim().replace(/\/+$/, '');
  if (!/^[^\s/\\]+(?:\/[^\s/\\]+)+$/.test(t)) return null;
  return { p: t, dir: !/\.\w{1,8}$/.test(t) };
};

DS.prose ??= {
  pathOf: demoPathOf,
  linkTargetOf: demoLinkTargetOf,
  /* Nothing to show but the file view itself: the demo has no filesystem, so
     both a file and a folder land on the same canned pane. */
  open: () => setWs(true, 'file'),
};
