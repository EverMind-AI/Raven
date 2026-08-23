/* -- workspace: the rpc DataSource + the prose chips' resolvers ----------
   The renderer is the workspace island (ui/src/features/workspace/); this
   file only knows how to speak fs.* over /rpc and how to resolve the paths
   an answer makes clickable. Installing onto the seam replaces the fixture
   source before the first paint. */

function relToWorkspace(p) {
  const s = String(p || '');
  const m = s.match(/(?:^|\/)(?:\.raven\/)?workspace\/(.+)$/);
  if (m) return m[1];
  if (s.startsWith('/') || s.startsWith('~')) return null;
  return s.replace(/^\.\//, '');
}

/* The session's working directory, learned from fs.list's answers -- the
   server resolves it and this side has no way to guess. */
let wsRoot = '';
const relToWsRoot = (p) => {
  const s = String(p || '');
  return wsRoot && s.startsWith(wsRoot + '/') ? s.slice(wsRoot.length + 1) : null;
};

/* "Looks like a path" is not enough to make one clickable: most repo-shaped
   strings in an answer are not files, and a link that opens onto an error is
   worse than plain text. Two cases are provable without a round trip: the text
   names the workspace, or Raven touched that file this session -- and since the
   viewer is no longer confined to the workspace, the second case now counts
   wherever the file lives. Everything else stays plain text. */
const livePathOf = (s) => {
  const t = String(s).trim().replace(/:\d+(?::\d+)?$/, '');
  if (!t || /\s/.test(t)) return null;
  if (/(?:^|\/)(?:\.raven\/)?workspace\/./.test(t)) return relToWorkspace(t) || t;
  const hit = WS.changes.find((c) => c.key === t || wsShortPath(c.key) === t);
  return hit ? hit.key : null;
};

/* An explicit markdown link is the author handing something over, so the live
   resolver is broader than the bare-span one above: absolute paths, ~ paths
   and workspace-relative shapes all count, extension decides file-or-folder.
   file:// is stripped rather than rejected -- models write it out of habit. */
const liveLinkTargetOf = (u) => {
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

/* Assigned, not ??=: the fixture source (demo/020-prose.js) is already on the
   seam by the time this runs, and replacing it before the first paint is the
   whole point. */
DS.prose = {
  pathOf: livePathOf,
  linkTargetOf: liveLinkTargetOf,
  open: ({ p, dir }) => (dir ? RavenIslands.workspace.openDir(p) : RavenIslands.workspace.showFile(p)),
};

DS.workspace = {
  canBrowse: true,
  list: (dir) => rpc.call('fs.list', { path: dir, session: sessionCurrent() || '' }).then((r) => {
    /* The root rides on every answer: it is the session's working directory,
       which shortPath below needs for every row the island asks about. */
    if (r.root) wsRoot = r.root;
    return r;
  }),
  reveal: (p) => rpc.call('fs.reveal', { path: p, session: sessionCurrent() || '' }),
  /* The other half of the viewer: a kind the page cannot render goes to the
     host's own application for it. `app` is a name the reader picked, or
     absent for the host default. Only offered while the gateway IS this
     desktop -- see hostIsLocal below. */
  openIn: (p, app) => rpc.call('fs.open', { path: p, session: sessionCurrent() || '', ...(app ? { app } : {}) }),
  /* Whether an application launched on the gateway's host would appear on the
     reader's own screen. `open` runs where the gateway runs, so on a remote
     serve these actions would start programs on somebody else's machine. */
  hostIsLocal: () => /^(127\.0\.0\.1|localhost|\[::1\])$/.test(location.hostname),
  shortPath: (p) => relToWsRoot(p) || relToWorkspace(p) || String(p).replace(/^\/Users\/[^/]+\//, '~/'),
};
