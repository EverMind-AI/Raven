/* -- the embedded browser: the rpc source -------------------------------
   The renderer is the browser island (ui-web/src/features/browser/); this file
   owns what only the live layer can: the browser.* calls on the DataSource
   seam, and the screencast frames the gateway pushes -- decoded here and
   forwarded through the source's onFrame hook the island subscribes to. */
/* Absent, not merely unusable. `avail: false` means the server has the surface
   and cannot use it right now (no chromium); this means the server does not
   have it at all -- a -32601 from any call. The set is shared: the browser
   island tracks its own surface, but the `subagent.*` source (230-tabs.js)
   still reads these. */
const RPC_ABSENT = new Set();
const rpcGone = (name, e) => {
  if (e && e.code === -32601) RPC_ABSENT.add(name);
  return RPC_ABSENT.has(name);
};
const rpcHas = (name) => !RPC_ABSENT.has(name);

DS.browser = {
  embedded: true,
  urls: () => RavenIslands.workspace.urls(),
  openUrl: (u) => RavenIslands.chrome.openUrl(u),
  frame: (p) => rpc.call('browser.frame', p),
  open: (p) => rpc.call('browser.open', p),
  watch: (p) => rpc.call('browser.watch', p),
  mode: (p) => rpc.call('browser.mode', p),
  close: () => rpc.call('browser.close', {}),
  tabs: (p) => rpc.call('browser.tabs', p),
  input: (p) => rpc.call('browser.input', p),
  onFrame: null,
};

const brB64Blob = (b64) => {
  const s = atob(b64);
  const u = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i);
  return new Blob([u], { type: 'image/jpeg' });
};

/* Screencast frames arrive as binary WS messages:
   "RVF1" + u32 header length + JSON header + raw JPEG. */
rpc.binary = (buf) => {
  const u8 = new Uint8Array(buf);
  if (u8.length < 8 || u8[0] !== 0x52 || u8[1] !== 0x56 || u8[2] !== 0x46 || u8[3] !== 0x31) return;
  const hl = new DataView(buf).getUint32(4);
  let head;
  try { head = JSON.parse(new TextDecoder().decode(u8.subarray(8, 8 + hl))); } catch { return; }
  if (DS.browser.onFrame) DS.browser.onFrame(head, new Blob([u8.subarray(8 + hl)], { type: 'image/jpeg' }));
};

/* Old servers still notify frames as base64 JSON; same hook after decode. */
rpc.notify['browser.frame'] = (p) => {
  if (DS.browser.onFrame) DS.browser.onFrame(p, p.jpeg ? brB64Blob(p.jpeg) : null);
};
