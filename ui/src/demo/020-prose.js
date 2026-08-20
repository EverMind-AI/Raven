/* ══ typed values in prose ═══════════════════════════════════════════
   An answer carries several kinds of value, and they are not the same thing to
   a reader: a URL is somewhere to go, a path is a file to look at, a command is
   text to copy. Each gets the affordance its kind earns and nothing more.

   The renderer is ui/src/shell/prose.ts, published as window.md. What stays
   here is the shell half: which strings resolve to something openable, what
   happens when the reader clicks one, and the fixture half of DS.prose.

   The rule for a path is that it is only ever clickable when it can actually be
   opened -- wsPathOf returns a workspace-relative path or null, and live.js
   replaces it with the resolver the workspace file view itself uses. A path
   that merely looks like one stays plain text: a dead link is worse than no
   link. Demo mode keeps a shape test so the canned transcript still reads. */
let wsPathOf = (s) => {
  const t = String(s).trim().replace(/:\d+(?::\d+)?$/, '');
  if (!/^[\w.@+-]+(?:\/[\w.@+-]+)+$/.test(t)) return null;
  return /\.\w{1,8}$/.test(t) ? t : null;
};
let pathOpen = (rel) => { setWs(true, 'file'); };
/* What a markdown link's local target resolves to: {p, dir} or null. Only for
   links the author wrote -- a bare word in prose gets no benefit of the doubt,
   while [打开交付文件夹](path/) is the author saying "this opens". A target
   without an extension is a folder; that is the whole dir test. */
let linkTargetOf = (u) => {
  const t = String(u).trim().replace(/\/+$/, '');
  if (!/^[^\s/\\]+(?:\/[^\s/\\]+)+$/.test(t)) return null;
  return { p: t, dir: !/\.\w{1,8}$/.test(t) };
};
let dirOpen = () => { setWs(true, 'file'); };

/* The two reads the renderer makes, wrapped rather than handed over: the live
   layer reassigns the bindings above and knows nothing about this seam, so
   these have to resolve at call time to see the replacement. */
DS.prose ??= {
  pathOf: (s) => wsPathOf(s),
  linkTargetOf: (u) => linkTargetOf(u),
};
