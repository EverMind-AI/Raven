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

import { islands } from '../../islands'
import { gateway } from '../../state/gateway'
import { sources } from '../../state/sources'

const RPC_ABSENT = new Set();
const rpcGone = (name, e) => {
  if (e && e.code === -32601) RPC_ABSENT.add(name);
  return RPC_ABSENT.has(name);
};
const rpcHas = (name) => !RPC_ABSENT.has(name);

const brB64Blob = (b64) => {
  const s = atob(b64);
  const u = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i);
  return new Blob([u], { type: 'image/jpeg' });
};

/* Screencast frames arrive as binary WS messages:
   "RVF1" + u32 header length + JSON header + raw JPEG. */
function onFrameBytes(buf) {
  const u8 = new Uint8Array(buf);
  if (u8.length < 8 || u8[0] !== 0x52 || u8[1] !== 0x56 || u8[2] !== 0x46 || u8[3] !== 0x31) return;
  const hl = new DataView(buf).getUint32(4);
  let head;
  try { head = JSON.parse(new TextDecoder().decode(u8.subarray(8, 8 + hl))); } catch { return; }
  if (sources.browser.onFrame) sources.browser.onFrame(head, new Blob([u8.subarray(8 + hl)], { type: 'image/jpeg' }));
}

/* Old servers still notify frames as base64 JSON; same hook after decode. */
function onFrameJson(p) {
  if (sources.browser.onFrame) sources.browser.onFrame(p, p.jpeg ? brB64Blob(p.jpeg) : null);
}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.browser = {
    embedded: true,
    urls: () => islands.workspace.urls(),
    openUrl: (u) => islands.chrome.openUrl(u),
    frame: (p) => gateway().call('browser.frame', p),
    open: (p) => gateway().call('browser.open', p),
    watch: (p) => gateway().call('browser.watch', p),
    mode: (p) => gateway().call('browser.mode', p),
    close: () => gateway().call('browser.close', {}),
    tabs: (p) => gateway().call('browser.tabs', p),
    input: (p) => gateway().call('browser.input', p),
    onFrame: null,
  };

  /* Screencast frames arrive as binary WS messages:
   "RVF1" + u32 header length + JSON header + raw JPEG. */
  gateway().binary(onFrameBytes);

  /* Old servers still notify frames as base64 JSON; same hook after decode. */
  gateway().on('browser.frame', onFrameJson);
}

export { RPC_ABSENT, rpcGone, rpcHas, brB64Blob, onFrameBytes, onFrameJson }
